# Original version copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.
"""
Tests for L{socks}, an implementation of the SOCKSv5 protocol with Tor
extension.
"""

import struct
import socket

from twisted.internet import defer, address
from twisted.internet.error import DNSLookupError
from twisted.python.compat import iterbytes
from twisted.test import proto_helpers
from twisted.trial import unittest

import socks


class StringTCPTransport(proto_helpers.StringTransport):
    disconnecting = False
    stringTCPTransport_closing = False
    peer = None

    def getPeer(self):
        return self.peer

    def getHost(self):
        return address.IPv4Address('TCP', '2.3.4.5', 42)

    def loseConnection(self):
        self.stringTCPTransport_closing = True
        self.disconnecting = True


class FakeResolverReactor:
    """
    Bare-bones reactor with deterministic behavior for the resolve method.
    """

    def __init__(self, names):
        """
        @type names: L{dict} containing L{str} keys and L{str} values.
        @param names: A hostname to IP address mapping. The IP addresses are
            stringified dotted quads.
        """
        self.names = names

    def resolve(self, hostname):
        """
        Resolve a hostname by looking it up in the C{names} dictionary.
        """
        try:
            return defer.succeed(self.names[hostname])
        except KeyError:
            return defer.fail(
                DNSLookupError(
                    "FakeResolverReactor couldn't find {}".format(hostname)
                )
            )


class SOCKSv5Driver(socks.SOCKSv5):
    # last SOCKSv5Outgoing instantiated
    driver_outgoing = None

    # last SOCKSv5IncomingFactory instantiated
    driver_listen = None

    def connectClass(self, host, port, klass, *args):
        # fake it
        def got_ip(ip):
            proto = klass(*args)
            transport = StringTCPTransport()
            transport.peer = address.IPv4Address('TCP', ip, port)
            proto.makeConnection(transport)
            self.driver_outgoing = proto
            return proto

        d = self.reactor.resolve(host)
        d.addCallback(got_ip)
        return d

    def listenClass(self, port, klass, *args):
        # fake it
        factory = klass(*args)
        self.driver_listen = factory
        if port == 0:
            port = 1234
        return defer.succeed(('6.7.8.9', port))


class ConnectTests(unittest.TestCase):
    """
    Tests for SOCKSv5 connect requests using the L{SOCKSv5} protocol.
    """

    def setUp(self):
        self.sock = SOCKSv5Driver()
        transport = StringTCPTransport()
        self.sock.makeConnection(transport)
        self.sock.reactor = FakeResolverReactor({
            "example.com": "5.6.7.8",
            "1.2.3.4": "1.2.3.4"
        })

    def deliver_data(self, protocol, data):
        """
        Deliver bytes one by one, to ensure parser can deal with unchunked
        data.
        """
        for byte in iterbytes(data):
            protocol.dataReceived(byte)

    def assert_handshake(self):
        """The server responds with NO_AUTH to the initial SOCKS5 handshake."""
        self.deliver_data(self.sock, struct.pack("!BBB", 5, 1, 0))
        reply = self.sock.transport.value()
        self.sock.transport.clear()
        self.assertEqual(reply, struct.pack("!BB", 5, 0))

    def assert_connect(self):
        """The server responds to CONNECT with successful result."""
        # The CONNECT command to an IPv4 address, host 1.2.3.4 port 34:
        # VER = 5, CMD = 1 (CONNECT), ATYP = 1 (IPv4)
        self.deliver_data(
            self.sock,
            struct.pack('!BBBB', 5, 1, 0, 1) + socket.inet_aton('1.2.3.4') +
            struct.pack("!H", 34)
        )
        reply = self.sock.transport.value()
        self.sock.transport.clear()
        self.assertEqual(
            reply,
            struct.pack('!BBBB', 5, 0, 0, 1) + socket.inet_aton('2.3.4.5') +
            struct.pack("!H", 42)
        )
        self.assertFalse(self.sock.transport.stringTCPTransport_closing)
        self.assertIsNotNone(self.sock.driver_outgoing)
        self.assertEqual(
            self.sock.driver_outgoing.transport.getPeer(),
            address.IPv4Address('TCP', '1.2.3.4', 34)
        )

    def assert_dataflow(self):
        """
        Data flows between client connection and proxied outgoing connection.
        """
        # pass some data through
        self.deliver_data(self.sock, b'hello, world')
        self.assertEqual(
            self.sock.driver_outgoing.transport.value(), b'hello, world'
        )

        # the other way around
        self.sock.driver_outgoing.dataReceived(b'hi there')
        self.assertEqual(self.sock.transport.value(), b'hi there')

    def test_simple(self):
        """The server proxies an outgoing connection to an IPv4 address."""
        self.assert_handshake()
        self.assert_connect()
        self.assert_dataflow()

        self.sock.connectionLost('fake reason')
        self.assertTrue(
            self.sock.driver_outgoing.transport.stringTCPTransport_closing
        )

    def test_socks5ConnectSuccessfulResolution(self):
        """
        Socks5 also supports hostname-based connections.

        @see: U{http://en.wikipedia.org/wiki/SOCKS#SOCKS_5_protocol}
        """
        self.assert_handshake()
        self.deliver_data(
            self.sock,
            struct.pack('!BBBB', 5, 0xf0, 0, 3) + struct.pack(
                "!B", len(b"example.com")
            ) + b"example.com" + struct.pack("!H", 3401)
        )
        reply = self.sock.transport.value()
        self.sock.transport.clear()
        self.assertEqual(
            reply,
            struct.pack('!BBBB', 5, 0, 0, 1) + socket.inet_aton('5.6.7.8')
        )
        self.assertTrue(self.sock.transport.stringTCPTransport_closing)

    def test_socks5TorStyleFailedResolution(self):
        """
        A Tor-style name resolution when resolution fails.
        """
        self.assert_handshake()
        self.deliver_data(
            self.sock,
            struct.pack('!BBBB', 5, 0xf0, 0, 3) + struct.pack(
                "!B", len(b"unknown")
            ) + b"unknown" + struct.pack("!H", 3401)
        )
        reply = self.sock.transport.value()
        self.sock.transport.clear()
        self.assertEqual(reply, struct.pack('!BBBB', 5, 4, 0, 0))
        self.assertTrue(self.sock.transport.stringTCPTransport_closing)
        self.assertEqual(len(self.flushLoggedErrors(DNSLookupError)), 1)

    def test_eofRemote(self):
        """If the outgoing connection closes the client connection closes."""
        self.assert_handshake()
        self.assert_connect()

        # now close it from the server side
        self.sock.driver_outgoing.connectionLost('fake reason')
        self.assertTrue(self.sock.transport.stringTCPTransport_closing)

    def test_eofLocal(self):
        """If the client connection closes the outgoing connection closes."""
        self.assert_handshake()
        self.assert_connect()

        self.sock.connectionLost('fake reason')
        self.assertTrue(
            self.sock.driver_outgoing.transport.stringTCPTransport_closing
        )
