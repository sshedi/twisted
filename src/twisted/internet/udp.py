# -*- test-case-name: twisted.test.test_udp -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Various asynchronous UDP classes.

Please do not use this module directly.

@var _sockErrReadIgnore: list of symbolic error constants (from the C{errno}
    module) representing socket errors where the error is temporary and can be
    ignored.

@var _sockErrReadRefuse: list of symbolic error constants (from the C{errno}
    module) representing socket errors that indicate connection refused.
"""


# System Imports
import socket
import warnings
from typing import Optional

from zope.interface import implementer

from twisted.internet._multicast import MulticastMixin
from twisted.internet.interfaces import IReactorMulticast
from twisted.internet.protocol import AbstractDatagramProtocol
from twisted.python.runtime import platformType

if platformType == "win32":
    from errno import WSAEINPROGRESS  # type: ignore[attr-defined]
    from errno import WSAEWOULDBLOCK  # type: ignore[attr-defined]
    from errno import (  # type: ignore[attr-defined]
        WSAECONNREFUSED,
        WSAECONNRESET,
        WSAEINTR,
        WSAEMSGSIZE,
        WSAENETRESET,
        WSAENOPROTOOPT as ENOPROTOOPT,
        WSAETIMEDOUT,
    )

    # Classify read and write errors
    _sockErrReadIgnore = [WSAEINTR, WSAEWOULDBLOCK, WSAEMSGSIZE, WSAEINPROGRESS]
    _sockErrReadRefuse = [WSAECONNREFUSED, WSAECONNRESET, WSAENETRESET, WSAETIMEDOUT]

    # POSIX-compatible write errors
    EMSGSIZE = WSAEMSGSIZE
    ECONNREFUSED = WSAECONNREFUSED
    EAGAIN = WSAEWOULDBLOCK
    EINTR = WSAEINTR
else:
    from errno import EAGAIN, ECONNREFUSED, EINTR, EMSGSIZE, ENOPROTOOPT, EWOULDBLOCK

    _sockErrReadIgnore = [EAGAIN, EINTR, EWOULDBLOCK]
    _sockErrReadRefuse = [ECONNREFUSED]

# Twisted Imports
from twisted.internet import abstract, address, base, defer, error, interfaces
from twisted.python import log


@implementer(
    interfaces.IListeningPort, interfaces.IUDPTransport, interfaces.ISystemHandle
)
class Port(base.BasePort):
    """
    UDP port, listening for packets.

    @ivar maxThroughput: Maximum number of bytes read in one event
        loop iteration.

    @ivar addressFamily: L{socket.AF_INET} or L{socket.AF_INET6}, depending on
        whether this port is listening on an IPv4 address or an IPv6 address.

    @ivar _realPortNumber: Actual port number being listened on. The
        value will be L{None} until this L{Port} is listening.

    @ivar _preexistingSocket: If not L{None}, a L{socket.socket} instance which
        was created and initialized outside of the reactor and will be used to
        listen for connections (instead of a new socket being created by this
        L{Port}).
    """

    addressFamily: socket.AddressFamily = socket.AF_INET
    socketType: socket.SocketKind = socket.SOCK_DGRAM
    maxThroughput = 256 * 1024

    _realPortNumber: Optional[int] = None
    _preexistingSocket = None

    def __init__(self, port, proto, interface="", maxPacketSize=8192, reactor=None):
        """
        @param port: A port number on which to listen.
        @type port: L{int}

        @param proto: A C{DatagramProtocol} instance which will be
            connected to the given C{port}.
        @type proto: L{twisted.internet.protocol.DatagramProtocol}

        @param interface: The local IPv4 or IPv6 address to which to bind;
            defaults to '', ie all IPv4 addresses.
        @type interface: L{str}

        @param maxPacketSize: The maximum packet size to accept.
        @type maxPacketSize: L{int}

        @param reactor: A reactor which will notify this C{Port} when
            its socket is ready for reading or writing. Defaults to
            L{None}, ie the default global reactor.
        @type reactor: L{interfaces.IReactorFDSet}
        """
        base.BasePort.__init__(self, reactor)
        self.port = port
        self.protocol = proto
        self.maxPacketSize = maxPacketSize
        self.interface = interface
        self.setLogStr()
        self._connectedAddr = None
        self._setAddressFamily()

    @classmethod
    def _fromListeningDescriptor(
        cls, reactor, fd, addressFamily, protocol, maxPacketSize
    ):
        """
        Create a new L{Port} based on an existing listening
        I{SOCK_DGRAM} socket.

        @param reactor: A reactor which will notify this L{Port} when
            its socket is ready for reading or writing. Defaults to
            L{None}, ie the default global reactor.
        @type reactor: L{interfaces.IReactorFDSet}

        @param fd: An integer file descriptor associated with a listening
            socket.  The socket must be in non-blocking mode.  Any additional
            attributes desired, such as I{FD_CLOEXEC}, must also be set already.
        @type fd: L{int}

        @param addressFamily: The address family (sometimes called I{domain}) of
            the existing socket.  For example, L{socket.AF_INET}.
        @type addressFamily: L{int}

        @param protocol: A C{DatagramProtocol} instance which will be
            connected to the C{port}.
        @type protocol: L{twisted.internet.protocol.DatagramProtocol}

        @param maxPacketSize: The maximum packet size to accept.
        @type maxPacketSize: L{int}

        @return: A new instance of C{cls} wrapping the socket given by C{fd}.
        @rtype: L{Port}
        """
        port = socket.fromfd(fd, addressFamily, cls.socketType)
        interface = port.getsockname()[0]
        self = cls(
            None,
            protocol,
            interface=interface,
            reactor=reactor,
            maxPacketSize=maxPacketSize,
        )
        self._preexistingSocket = port
        return self

    def __repr__(self) -> str:
        if self._realPortNumber is not None:
            return f"<{self.protocol.__class__} on {self._realPortNumber}>"
        else:
            return f"<{self.protocol.__class__} not connected>"

    def getHandle(self):
        """
        Return a socket object.
        """
        return self.socket

    def startListening(self):
        """
        Create and bind my socket, and begin listening on it.

        This is called on unserialization, and must be called after creating a
        server to begin listening on the specified port.
        """
        self._bindSocket()
        self._connectToProtocol()

    def _bindSocket(self):
        """
        Prepare and assign a L{socket.socket} instance to
        C{self.socket}.

        Either creates a new SOCK_DGRAM L{socket.socket} bound to
        C{self.interface} and C{self.port} or takes an existing
        L{socket.socket} provided via the
        L{interfaces.IReactorSocket.adoptDatagramPort} interface.
        """
        if self._preexistingSocket is None:
            # Create a new socket and make it listen
            try:
                skt = self.createInternetSocket()
                skt.bind((self.interface, self.port))
            except OSError as le:
                raise error.CannotListenError(self.interface, self.port, le)
        else:
            # Re-use the externally specified socket
            skt = self._preexistingSocket
            self._preexistingSocket = None

        # Make sure that if we listened on port 0, we update that to
        # reflect what the OS actually assigned us.
        self._realPortNumber = skt.getsockname()[1]

        log.msg(
            "%s starting on %s"
            % (self._getLogPrefix(self.protocol), self._realPortNumber)
        )

        self.connected = 1
        self.socket = skt
        self.fileno = self.socket.fileno

    def _connectToProtocol(self):
        self.protocol.makeConnection(self)
        self.startReading()

    def doRead(self):
        """
        Called when my socket is ready for reading.
        """
        read = 0
        while read < self.maxThroughput:
            try:
                data, addr = self.socket.recvfrom(self.maxPacketSize)
            except OSError as se:
                no = se.args[0]
                if no in _sockErrReadIgnore:
                    return
                if no in _sockErrReadRefuse:
                    if self._connectedAddr:
                        self.protocol.connectionRefused()
                    return
                raise
            else:
                read += len(data)
                if self.addressFamily == socket.AF_INET6:
                    # Remove the flow and scope ID from the address tuple,
                    # reducing it to a tuple of just (host, port).
                    #
                    # TODO: This should be amended to return an object that can
                    # unpack to (host, port) but also includes the flow info
                    # and scope ID. See http://tm.tl/6826
                    addr = addr[:2]
                try:
                    self.protocol.datagramReceived(data, addr)
                except BaseException:
                    log.err()

    def write(self, datagram, addr=None):
        """
        Write a datagram.

        @type datagram: L{bytes}
        @param datagram: The datagram to be sent.

        @type addr: L{tuple} containing L{str} as first element and L{int} as
            second element, or L{None}
        @param addr: A tuple of (I{stringified IPv4 or IPv6 address},
            I{integer port number}); can be L{None} in connected mode.
        """
        if self._connectedAddr:
            assert addr in (None, self._connectedAddr)
            try:
                return self.socket.send(datagram)
            except OSError as se:
                no = se.args[0]
                if no == EINTR:
                    return self.write(datagram)
                elif no == EMSGSIZE:
                    raise error.MessageLengthError("message too long")
                elif no == ECONNREFUSED:
                    self.protocol.connectionRefused()
                else:
                    raise
        else:
            assert addr != None
            if (
                not abstract.isIPAddress(addr[0])
                and not abstract.isIPv6Address(addr[0])
                and addr[0] != "<broadcast>"
            ):
                raise error.InvalidAddressError(
                    addr[0], "write() only accepts IP addresses, not hostnames"
                )
            if (
                abstract.isIPAddress(addr[0]) or addr[0] == "<broadcast>"
            ) and self.addressFamily == socket.AF_INET6:
                raise error.InvalidAddressError(
                    addr[0], "IPv6 port write() called with IPv4 or broadcast address"
                )
            if abstract.isIPv6Address(addr[0]) and self.addressFamily == socket.AF_INET:
                raise error.InvalidAddressError(
                    addr[0], "IPv4 port write() called with IPv6 address"
                )
            try:
                return self.socket.sendto(datagram, addr)
            except OSError as se:
                no = se.args[0]
                if no == EINTR:
                    return self.write(datagram, addr)
                elif no == EMSGSIZE:
                    raise error.MessageLengthError("message too long")
                elif no == ECONNREFUSED:
                    # in non-connected UDP ECONNREFUSED is platform dependent, I
                    # think and the info is not necessarily useful. Nevertheless
                    # maybe we should call connectionRefused? XXX
                    return
                else:
                    raise

    def writeSequence(self, seq, addr):
        """
        Write a datagram constructed from an iterable of L{bytes}.

        @param seq: The data that will make up the complete datagram to be
            written.
        @type seq: an iterable of L{bytes}

        @type addr: L{tuple} containing L{str} as first element and L{int} as
            second element, or L{None}
        @param addr: A tuple of (I{stringified IPv4 or IPv6 address},
            I{integer port number}); can be L{None} in connected mode.
        """
        self.write(b"".join(seq), addr)

    def connect(self, host, port):
        """
        'Connect' to remote server.
        """
        if self._connectedAddr:
            raise RuntimeError(
                "already connected, reconnecting is not currently supported"
            )
        if not abstract.isIPAddress(host) and not abstract.isIPv6Address(host):
            raise error.InvalidAddressError(host, "not an IPv4 or IPv6 address.")
        self._connectedAddr = (host, port)
        self.socket.connect((host, port))

    def _loseConnection(self):
        self.stopReading()
        if self.connected:  # actually means if we are *listening*
            self.reactor.callLater(0, self.connectionLost)

    def stopListening(self):
        if self.connected:
            result = self.d = defer.Deferred()
        else:
            result = None
        self._loseConnection()
        return result

    def loseConnection(self):
        warnings.warn(
            "Please use stopListening() to disconnect port",
            DeprecationWarning,
            stacklevel=2,
        )
        self.stopListening()

    def connectionLost(self, reason=None):
        """
        Cleans up my socket.
        """
        log.msg("(UDP Port %s Closed)" % self._realPortNumber)
        self._realPortNumber = None
        self.maxThroughput = -1
        base.BasePort.connectionLost(self, reason)
        self.protocol.doStop()
        self.socket.close()
        del self.socket
        del self.fileno
        if hasattr(self, "d"):
            self.d.callback(None)
            del self.d

    def setLogStr(self):
        """
        Initialize the C{logstr} attribute to be used by C{logPrefix}.
        """
        logPrefix = self._getLogPrefix(self.protocol)
        self.logstr = "%s (UDP)" % logPrefix

    def _setAddressFamily(self):
        """
        Resolve address family for the socket.
        """
        if abstract.isIPv6Address(self.interface):
            self.addressFamily = socket.AF_INET6
        elif abstract.isIPAddress(self.interface):
            self.addressFamily = socket.AF_INET
        elif self.interface:
            raise error.InvalidAddressError(
                self.interface, "not an IPv4 or IPv6 address."
            )

    def logPrefix(self):
        """
        Return the prefix to log with.
        """
        return self.logstr

    def getHost(self):
        """
        Return the local address of the UDP connection

        @returns: the local address of the UDP connection
        @rtype: L{IPv4Address} or L{IPv6Address}
        """
        addr = self.socket.getsockname()
        if self.addressFamily == socket.AF_INET:
            return address.IPv4Address("UDP", *addr)
        elif self.addressFamily == socket.AF_INET6:
            return address.IPv6Address("UDP", *(addr[:2]))

    def setBroadcastAllowed(self, enabled):
        """
        Set whether this port may broadcast. This is disabled by default.

        @param enabled: Whether the port may broadcast.
        @type enabled: L{bool}
        """
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, enabled)

    def getBroadcastAllowed(self):
        """
        Checks if broadcast is currently allowed on this port.

        @return: Whether this port may broadcast.
        @rtype: L{bool}
        """
        return bool(self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST))


@implementer(interfaces.IMulticastTransport)
class MulticastPort(MulticastMixin, Port):
    """
    UDP Port that supports multicasting.
    """

    def __init__(
        self,
        port: int,
        proto: AbstractDatagramProtocol,
        interface: str = "",
        maxPacketSize: int = 8192,
        reactor: IReactorMulticast | None = None,
        listenMultiple: bool = False,
    ) -> None:
        """
        @see: L{twisted.internet.interfaces.IReactorMulticast.listenMulticast}
        """
        Port.__init__(self, port, proto, interface, maxPacketSize, reactor)
        self.listenMultiple = listenMultiple

    def createInternetSocket(self) -> socket.socket:
        skt = Port.createInternetSocket(self)
        if self.listenMultiple:
            skt.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    skt.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError as le:
                    # RHEL6 defines SO_REUSEPORT but it doesn't work
                    if le.errno == ENOPROTOOPT:
                        pass
                    else:
                        raise
        return skt

    def _bindSocket(self):
        bound = super()._bindSocket()
        return bound
