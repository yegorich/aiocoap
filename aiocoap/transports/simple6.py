# This file is part of the Python aiocoap library project.
#
# Copyright (c) 2012-2014 Maciej Wasilak <http://sixpinetrees.blogspot.com/>,
#               2013-2014 Christian Amsüss <c.amsuess@energyharvesting.at>
#
# aiocoap is free software, this file is published under the MIT license as
# described in the accompanying LICENSE file.

"""This module implements a TransportEndpoint for UDP based on the asyncio
DatagramProtocol.

This is a simple version that works only for clients (by creating a dedicated
unbound but connected socket for each communication partner) and probably not
with multicast (it is assumed to be unsafe for multicast), which can be
expected to work even on platforms where the :mod:`.udp6` module can not be
made to work (Android, OSX, Windows for missing ``recvmsg`` and socket options,
uvloop because :class:`.util.asyncio.RecvmsgSelectorDatagramTransport` is not
implemented there).

This transport is experimental, likely to change, and not fully tested yet
(because the test suite is not yet ready to matrix-test the same tests with
different transport implementations, and because it still fails in proxy
blockwise tests).
"""

import urllib
import asyncio
import socket

from aiocoap import interfaces, error
from aiocoap import Message, COAP_PORT
from ..util import hostportjoin

class _Connection(asyncio.DatagramProtocol):
    # FIXME this should have the same inteface as udp6.UDP6EndpointAddress

    def __init__(self, ready_callback, new_message_callback, new_error_callback):
        self._ready_callback = ready_callback
        self._new_message_callback = new_message_callback
        self._new_error_callback = new_error_callback

        self._stage = "initializing" #: Status property purely for debugging

    def __repr__(self):
        return "<%s at %#x on transport %s, %s>" % (
                type(self).__name__,
                id(self),
                getattr(self, "_transport", "(none)"),
                self._stage)

    # address interface

    @property
    def is_multicast(self):
        return False

    # FIXME is this / should this really be part of the interface, or does it
    # just happen to be tested by the unit tests?
    @property
    def port(self):
        return self._transport.get_extra_info('socket').getpeername()[1]

    @property
    def hostinfo(self):
        host = self._transport.get_extra_info('socket').getpeername()[0]
        port = self.port
        if port == COAP_PORT:
            port = None
        # FIXME this should use some of the _plainaddress mechanisms of the udp6 addresses
        return hostportjoin(host, port)

    # datagram protocol interface

    def connection_made(self, transport):
        self._transport = transport
        self._ready_callback()
        self._stage = "active"
        del self._ready_callback

    def datagram_received(self, data, address):
        self._new_message_callback(self, data)

    def error_received(self, exception):
        self._new_error_callback(self, exception)

    def connection_lost(self, exception):
        if exception is None:
            pass
        else:
            self._new_error_callback(self, exception)

    # whatever it is _DatagramSocketpoolSimple6 expects

    def send(self, data):
        self._transport.sendto(data, None)

    @asyncio.coroutine
    def shutdown(self):
        self._stage = "shutting down"
        self._transport.abort()
        del self._new_message_callback
        del self._new_error_callback
        self._stage = "destroyed"

class _DatagramSocketpoolSimple6:
    """This class is used to explore what an Python/asyncio abstraction around
    a hypothetical "UDP connections" mechanism could look like.

    Assume there were a socket variety that had UDP messages (ie. unreliable,
    unordered, boundary-preserving) but that can do an accept() like a TCP
    listening socket can, and can create outgoing connection-ish sockets from
    the listeing port.

    That interface would be usable for all UDP-based CoAP transport
    implementations; this particular implementation, due to limitations of
    POSIX sockets (and the additional limitations imposed on it like not using
    PKTINFO) provides the interface, but only implements the outgoing part, and
    will not allow setting the outgoing port or interface."""

    def __init__(self):
        # currently tracked only for shutdown
        self._sockets = []

    # FIXME (new_message_callback, new_error_callback) should probably rather
    # be one object with a defined interface; either that's the
    # TransportEndpointSimple6 and stored accessibly (so the Protocol can know
    # which TransportEndpoint to talk to for sending), or we move the
    # TransportEndpoint out completely and have that object be the Protocol,
    # and the Protocol can even send new packages via the address
    @asyncio.coroutine
    def connect(self, sockaddr, loop, new_message_callback, new_error_callback):
        """Create a new socket with a given remote socket address

        Note that the sockaddr does not need to be fully resolved or complete,
        as it is not used for matching incoming packages; ('host.example.com',
        5683) is perfectly OK (and will create a different outgoing socket that
        ('hostalias.example.com', 5683) even if that has the same address, for
        better or for worse).

        For where the general underlying interface is concerned, it is not yet
        fixed at all when this must return identical objects."""

        ready = asyncio.Future()
        transport, protocol = yield from loop.create_datagram_endpoint(
                lambda: _Connection(lambda: ready.set_result(None), new_message_callback, new_error_callback),
                family=socket.AF_INET6,
                remote_addr=sockaddr)
        yield from ready

        # FIXME twice: 1., those never get removed yet (should timeout or
        # remove themselves on error), and 2., this is racy against a shutdown right after a connect
        self._sockets.append(protocol)

        return protocol

    @asyncio.coroutine
    def shutdown(self):
        if self._sockets:
            yield from asyncio.wait([s.shutdown() for s in self._sockets])
        del self._sockets

class TransportEndpointSimple6(interfaces.TransportEndpoint):
    # Ideally, this should be a generic "TransportEndpoint implemented atop
    # something like _DatagramSocketpoolSimple6"; adding "FIXME specific" where
    # this is not the case.
    def __init__(self, new_message_callback, new_error_callback, log, loop):
        self._new_message_callback = new_message_callback
        self._new_error_callback = new_error_callback
        self._log = log
        self._loop = loop

        # FIXME specific, but only for class
        self._pool = _DatagramSocketpoolSimple6()

    @asyncio.coroutine
    def determine_remote(self, request):
        if request.requested_scheme not in ('coap', None):
            return None

        if request.unresolved_remote is not None:
            pseudoparsed = urllib.parse.SplitResult(None, request.unresolved_remote, None, None, None)
            host = pseudoparsed.hostname
            port = pseudoparsed.port or COAP_PORT
        elif request.opt.uri_host:
            host = request.opt.uri_host
            port = request.opt.uri_port or COAP_PORT
        else:
            raise ValueError("No location found to send message to (neither in .opt.uri_host nor in .remote)")

        return (yield from self._pool.connect((host, port), self._loop, self._received_datagram, self._received_exception))

    def _received_datagram(self, address, datagram):
        try:
            message = Message.decode(datagram, remote=address)
        except error.UnparsableMessage:
            self._log.warning("Ignoring unparsable message from %s"%(address,))
            return

        self._new_message_callback(message)

    def _received_exception(self, address, exception):
        self._new_error_callback(exception.errno, address)

    def send(self, message):
        message.remote.send(message.encode())

    @asyncio.coroutine
    def shutdown(self):
        yield from self._pool.shutdown()
        self._new_message_callback = None
        self._new_error_callback = None
