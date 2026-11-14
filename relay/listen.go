package main

import (
	"bufio"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const (
	socksVersion       = 5
	socksCmdConnect    = 1
	socksAtypIPv4      = 1
	socksAtypDomain    = 3
	socksAtypIPv6      = 4
	socksSucceeded     = 0
	socksGeneralFail   = 1
	socksNotAllowed    = 2
	socksRefused       = 5
	socksCmdNotSupport = 7
	socksAtypNotSupp   = 8

	openTimeout      = 10 * time.Second
	handshakeTimeout = 10 * time.Second
)

var errRefused = errors.New("destination refused")

type listener struct {
	m           *mux
	egressProxy string
}

func newListener(m *mux, egressProxy string) *listener {
	return &listener{m: m, egressProxy: egressProxy}
}

func (l *listener) accept(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		go l.handle(conn)
	}
}

func (l *listener) demux(in io.Reader) error {
	defer l.m.closeAll()
	for {
		f, err := readFrame(in)
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			return nil
		}
		if err != nil {
			return err
		}
		if l.m.dispatch(f) {
			continue
		}
		if f.kind == frameOpenOK || f.kind == frameOpenFail {
			l.m.mu.Lock()
			ch := l.m.pending[f.stream]
			delete(l.m.pending, f.stream)
			l.m.mu.Unlock()
			if ch != nil {
				ch <- f.kind == frameOpenOK
			}
		}
	}
}

func isLoopback(host string) bool {
	h := strings.ToLower(strings.TrimSuffix(host, "."))
	if h == "localhost" || strings.HasSuffix(h, ".localhost") {
		return true
	}
	ip := net.ParseIP(h)
	return ip != nil && ip.IsLoopback()
}

func (l *listener) handle(conn net.Conn) {
	_ = conn.SetDeadline(time.Now().Add(handshakeTimeout))
	host, port, err := socksHandshake(conn)
	if err != nil {
		_ = conn.Close()
		return
	}
	if isLoopback(host) {
		l.relayToSandbox(conn, port)
		return
	}
	if l.egressProxy == "" {
		socksReply(conn, socksNotAllowed)
		_ = conn.Close()
		return
	}
	l.relayToProxy(conn, host, port)
}

func (l *listener) relayToSandbox(conn net.Conn, port uint16) {
	result := make(chan bool, 1)
	l.m.mu.Lock()
	id := l.m.nextID
	l.m.nextID += 2
	l.m.pending[id] = result
	l.m.mu.Unlock()
	s := l.m.register(id, conn)

	if err := l.m.send(frame{kind: frameOpen, stream: id, payload: portPayload(port)}); err != nil {
		l.refuse(conn, id, socksGeneralFail, false)
		return
	}
	var ok bool
	select {
	case ok = <-result:
	case <-time.After(openTimeout):
		l.refuse(conn, id, socksGeneralFail, true)
		return
	}
	if !ok {
		l.refuse(conn, id, socksRefused, false)
		return
	}
	socksReply(conn, socksSucceeded)
	_ = conn.SetDeadline(time.Time{})
	s.start()
}

func (l *listener) refuse(conn net.Conn, id uint32, code byte, notifyPeer bool) {
	l.m.mu.Lock()
	delete(l.m.pending, id)
	delete(l.m.streams, id)
	l.m.mu.Unlock()
	if notifyPeer {
		_ = l.m.send(frame{kind: frameReset, stream: id})
	}
	socksReply(conn, code)
	_ = conn.Close()
}

func (l *listener) relayToProxy(conn net.Conn, host string, port uint16) {
	upstream, buffered, err := dialThroughProxy(l.egressProxy, host, port)
	if err != nil {
		code := byte(socksGeneralFail)
		if errors.Is(err, errRefused) {
			code = socksNotAllowed
		}
		socksReply(conn, code)
		_ = conn.Close()
		return
	}
	socksReply(conn, socksSucceeded)
	_ = conn.SetDeadline(time.Time{})
	if len(buffered) > 0 {
		if _, err := conn.Write(buffered); err != nil {
			_ = conn.Close()
			_ = upstream.Close()
			return
		}
	}
	pipe(conn, upstream)
}

func dialThroughProxy(proxy, host string, port uint16) (net.Conn, []byte, error) {
	upstream, err := net.DialTimeout("tcp", proxy, handshakeTimeout)
	if err != nil {
		return nil, nil, err
	}
	_ = upstream.SetDeadline(time.Now().Add(handshakeTimeout))
	target := net.JoinHostPort(host, strconv.Itoa(int(port)))
	if _, err := fmt.Fprintf(upstream, "CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n", target, target); err != nil {
		_ = upstream.Close()
		return nil, nil, err
	}
	br := bufio.NewReader(upstream)
	resp, err := http.ReadResponse(br, &http.Request{Method: http.MethodConnect})
	if err != nil {
		_ = upstream.Close()
		return nil, nil, err
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_ = upstream.Close()
		return nil, nil, fmt.Errorf("%w: proxy answered %d", errRefused, resp.StatusCode)
	}
	_ = upstream.SetDeadline(time.Time{})
	buffered, _ := br.Peek(br.Buffered())
	return upstream, append([]byte(nil), buffered...), nil
}

func pipe(a, b net.Conn) {
	done := make(chan struct{}, 2)
	copyHalf := func(dst, src net.Conn) {
		_, _ = io.Copy(dst, src)
		if tcp, ok := dst.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
		done <- struct{}{}
	}
	go copyHalf(a, b)
	go copyHalf(b, a)
	<-done
	<-done
	_ = a.Close()
	_ = b.Close()
}

func socksHandshake(conn net.Conn) (string, uint16, error) {
	var head [2]byte
	if _, err := io.ReadFull(conn, head[:]); err != nil {
		return "", 0, err
	}
	if head[0] != socksVersion {
		return "", 0, fmt.Errorf("unsupported SOCKS version %d", head[0])
	}
	methods := make([]byte, head[1])
	if _, err := io.ReadFull(conn, methods); err != nil {
		return "", 0, err
	}
	if _, err := conn.Write([]byte{socksVersion, 0}); err != nil {
		return "", 0, err
	}
	var req [4]byte
	if _, err := io.ReadFull(conn, req[:]); err != nil {
		return "", 0, err
	}
	if req[1] != socksCmdConnect {
		socksReply(conn, socksCmdNotSupport)
		return "", 0, fmt.Errorf("unsupported SOCKS command %d", req[1])
	}
	var host string
	switch req[3] {
	case socksAtypIPv4:
		addr := make([]byte, 4)
		if _, err := io.ReadFull(conn, addr); err != nil {
			return "", 0, err
		}
		host = net.IP(addr).String()
	case socksAtypIPv6:
		addr := make([]byte, 16)
		if _, err := io.ReadFull(conn, addr); err != nil {
			return "", 0, err
		}
		host = net.IP(addr).String()
	case socksAtypDomain:
		var size [1]byte
		if _, err := io.ReadFull(conn, size[:]); err != nil {
			return "", 0, err
		}
		name := make([]byte, size[0])
		if _, err := io.ReadFull(conn, name); err != nil {
			return "", 0, err
		}
		host = string(name)
	default:
		socksReply(conn, socksAtypNotSupp)
		return "", 0, fmt.Errorf("unsupported SOCKS address type %d", req[3])
	}
	var portBytes [2]byte
	if _, err := io.ReadFull(conn, portBytes[:]); err != nil {
		return "", 0, err
	}
	return host, binary.BigEndian.Uint16(portBytes[:]), nil
}

func socksReply(conn net.Conn, code byte) {
	_, _ = conn.Write([]byte{socksVersion, code, 0, socksAtypIPv4, 0, 0, 0, 0, 0, 0})
}
