package main

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestFrameRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	sent := frame{kind: frameData, stream: 7, payload: []byte("hello")}
	if err := writeFrame(&buf, sent); err != nil {
		t.Fatal(err)
	}
	if err := writeFrame(&buf, frame{kind: frameClose, stream: 7}); err != nil {
		t.Fatal(err)
	}
	got, err := readFrame(&buf)
	if err != nil {
		t.Fatal(err)
	}
	if got.kind != sent.kind || got.stream != sent.stream || string(got.payload) != "hello" {
		t.Fatalf("got %+v, want %+v", got, sent)
	}
	got, err = readFrame(&buf)
	if err != nil || got.kind != frameClose || got.stream != 7 || len(got.payload) != 0 {
		t.Fatalf("got %+v (%v), want an empty close frame", got, err)
	}
}

func TestFrameRejectsOversizedPayload(t *testing.T) {
	if err := writeFrame(io.Discard, frame{kind: frameData, payload: make([]byte, maxPayload+1)}); err == nil {
		t.Fatal("writeFrame accepted an oversized payload")
	}
	header := make([]byte, headerSize)
	header[0] = frameData
	binary.BigEndian.PutUint32(header[5:9], maxPayload+1)
	if _, err := readFrame(bytes.NewReader(header)); err == nil {
		t.Fatal("readFrame accepted an oversized payload")
	}
}

func TestIsLoopback(t *testing.T) {
	cases := map[string]bool{
		"localhost": true, "LOCALHOST.": true, "app.localhost": true, "127.0.0.1": true, "127.1.2.3": true,
		"::1": true, "example.com": false, "localhost.example.com": false, "10.0.0.1": false, "::": false,
	}
	for host, want := range cases {
		if got := isLoopback(host); got != want {
			t.Errorf("isLoopback(%q) = %v, want %v", host, got, want)
		}
	}
}

func echoServer(t *testing.T, network, addr string) net.Listener {
	t.Helper()
	ln, err := net.Listen(network, addr)
	if err != nil {
		t.Skipf("cannot listen on %s %s: %v", network, addr, err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				_, _ = io.Copy(conn, conn)
			}()
		}
	}()
	return ln
}

func portOf(ln net.Listener) uint16 {
	return uint16(ln.Addr().(*net.TCPAddr).Port)
}

// startRelay wires a listener and a server back to back, as daiv-sandbox's exec pump does.
func startRelay(t *testing.T, egressProxy string) string {
	t.Helper()
	toServer, fromListener := io.Pipe()
	toListener, fromServer := io.Pipe()
	l := newListener(newMux(fromListener), egressProxy)
	go func() { _ = newServer(newMux(fromServer)).demux(toServer) }()
	go func() { _ = l.demux(toListener) }()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go l.accept(ln)
	t.Cleanup(func() {
		ln.Close()
		fromListener.Close()
		fromServer.Close()
	})
	return ln.Addr().String()
}

func socksConnect(t *testing.T, relayAddr, host string, port uint16) (net.Conn, byte) {
	t.Helper()
	conn, err := net.DialTimeout("tcp", relayAddr, 5*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.SetDeadline(time.Now().Add(15 * time.Second))
	req := []byte{5, 1, 0, 5, 1, 0, socksAtypDomain, byte(len(host))}
	req = append(req, host...)
	req = binary.BigEndian.AppendUint16(req, port)
	if _, err := conn.Write(req); err != nil {
		t.Fatal(err)
	}
	reply := make([]byte, 12)
	if _, err := io.ReadFull(conn, reply); err != nil {
		t.Fatal(err)
	}
	if reply[0] != 5 || reply[1] != 0 {
		t.Fatalf("bad method selection %v", reply[:2])
	}
	return conn, reply[3]
}

func assertEcho(t *testing.T, conn net.Conn, msg string) {
	t.Helper()
	if _, err := conn.Write([]byte(msg)); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, len(msg))
	if _, err := io.ReadFull(conn, got); err != nil {
		t.Fatal(err)
	}
	if string(got) != msg {
		t.Fatalf("echo got %q, want %q", got, msg)
	}
}

func TestLoopbackDestinationReachesTheSandboxService(t *testing.T) {
	relay := startRelay(t, "")
	echo := echoServer(t, "tcp", "127.0.0.1:0")
	for _, host := range []string{"localhost", "127.0.0.1", "app.localhost"} {
		conn, code := socksConnect(t, relay, host, portOf(echo))
		if code != socksSucceeded {
			t.Fatalf("%s: SOCKS reply %d, want success", host, code)
		}
		assertEcho(t, conn, "ping "+host)
		assertEcho(t, conn, strings.Repeat("x", 3*maxPayload))
		conn.Close()
	}
}

func TestLoopbackStreamsAreIndependent(t *testing.T) {
	relay := startRelay(t, "")
	echo := echoServer(t, "tcp", "127.0.0.1:0")
	a, _ := socksConnect(t, relay, "localhost", portOf(echo))
	b, _ := socksConnect(t, relay, "localhost", portOf(echo))
	assertEcho(t, b, "second")
	assertEcho(t, a, "first")
	a.Close()
	assertEcho(t, b, "still open")
	b.Close()
}

// firehoseServer writes to every connection until the write fails, counting the bytes it got out.
func firehoseServer(t *testing.T) (net.Listener, *atomic.Int64) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	var sent atomic.Int64
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				chunk := make([]byte, maxPayload)
				for {
					n, err := conn.Write(chunk)
					sent.Add(int64(n))
					if err != nil {
						return
					}
				}
			}()
		}
	}()
	return ln, &sent
}

func waitUntilStalled(t *testing.T, sent *atomic.Int64) {
	t.Helper()
	last := int64(-1)
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); {
		time.Sleep(300 * time.Millisecond)
		now := sent.Load()
		if now == last {
			return
		}
		last = now
	}
	t.Fatal("the firehose never stalled")
}

func TestAStreamNobodyReadsDoesNotStallTheOthers(t *testing.T) {
	relay := startRelay(t, "")
	firehose, sent := firehoseServer(t)
	echo := echoServer(t, "tcp", "127.0.0.1:0")

	unread, code := socksConnect(t, relay, "localhost", portOf(firehose))
	defer unread.Close()
	if code != socksSucceeded {
		t.Fatalf("SOCKS reply %d, want success", code)
	}
	waitUntilStalled(t, sent)

	conn, code := socksConnect(t, relay, "localhost", portOf(echo))
	defer conn.Close()
	if code != socksSucceeded {
		t.Fatalf("SOCKS reply %d, want success", code)
	}
	assertEcho(t, conn, strings.Repeat("y", 3*maxPayload))
}

func TestAStreamResetWhileItDialsIsClosedAndNeverOpened(t *testing.T) {
	in, feed := io.Pipe()
	outR, outW := io.Pipe()
	srv := newServer(newMux(outW))
	release := make(chan struct{})
	local, remote := net.Pipe()
	srv.dial = func(uint16) (net.Conn, error) {
		<-release
		return local, nil
	}
	go func() { _ = srv.demux(in) }()
	frames := make(chan frame, 4)
	go func() {
		for {
			f, err := readFrame(outR)
			if err != nil {
				return
			}
			frames <- f
		}
	}()

	for _, f := range []frame{
		{kind: frameOpen, stream: 1, payload: portPayload(8080)},
		{kind: frameReset, stream: 1},
		{kind: frameClose, stream: 99},
	} {
		if err := writeFrame(feed, f); err != nil {
			t.Fatal(err)
		}
	}
	close(release)

	_ = remote.SetReadDeadline(time.Now().Add(5 * time.Second))
	if _, err := remote.Read(make([]byte, 1)); !errors.Is(err, io.EOF) {
		t.Fatalf("the dialled connection was left open: %v", err)
	}
	select {
	case f := <-frames:
		t.Fatalf("a reset stream was answered with %+v", f)
	case <-time.After(200 * time.Millisecond):
	}
	if srv.m.lookup(1) != nil {
		t.Fatal("a reset stream was registered")
	}
	feed.Close()
}

func TestAPeerThatSendsPastItsWindowIsReset(t *testing.T) {
	var out bytes.Buffer
	m := newMux(&out)
	local, remote := net.Pipe()
	defer remote.Close()
	s := m.register(9, local)
	for sent := 0; sent < streamWindow; sent += maxPayload {
		s.deliver(make([]byte, maxPayload))
	}
	if m.lookup(9) == nil {
		t.Fatal("a peer within its window was reset")
	}
	s.deliver([]byte{1})
	if m.lookup(9) != nil {
		t.Fatal("a peer past its window was not reset")
	}
	if got, err := readFrame(&out); err != nil || got.kind != frameReset || got.stream != 9 {
		t.Fatalf("got %+v (%v), want a reset for stream 9", got, err)
	}
}

func TestDialLoopbackFallsBackToIPv6(t *testing.T) {
	echo := echoServer(t, "tcp6", "[::1]:0")
	if c, err := net.Dial("tcp4", net.JoinHostPort("127.0.0.1", strconv.Itoa(int(portOf(echo))))); err == nil {
		c.Close()
		t.Skip("the IPv4 loopback port is taken by another listener")
	}
	conn, err := dialLoopback(portOf(echo))
	if err != nil {
		t.Fatalf("dialLoopback did not fall back to ::1: %v", err)
	}
	defer conn.Close()
	if !strings.HasPrefix(conn.RemoteAddr().String(), "[::1]") {
		t.Fatalf("connected to %s, want ::1", conn.RemoteAddr())
	}
}

func TestLoopbackPortWithNothingListeningIsRefused(t *testing.T) {
	relay := startRelay(t, "")
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	port := portOf(ln)
	ln.Close()
	conn, code := socksConnect(t, relay, "localhost", port)
	defer conn.Close()
	if code != socksRefused {
		t.Fatalf("SOCKS reply %d, want %d (refused)", code, socksRefused)
	}
}

func TestNonLoopbackIsRefusedWithoutAnEgressProxy(t *testing.T) {
	relay := startRelay(t, "")
	conn, code := socksConnect(t, relay, "example.com", 443)
	defer conn.Close()
	if code != socksNotAllowed {
		t.Fatalf("SOCKS reply %d, want %d (not allowed)", code, socksNotAllowed)
	}
}

// fakeProxy answers CONNECT for allowedTarget with 200 and an echo, and 403 for anything else.
func fakeProxy(t *testing.T, allowedTarget string) (string, chan string) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	seen := make(chan string, 8)
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				br := bufio.NewReader(conn)
				req, err := http.ReadRequest(br)
				if err != nil {
					return
				}
				seen <- req.Method + " " + req.Host + " " + req.Header.Get("X-Daiv-Client")
				if req.Method != http.MethodConnect || req.Host != allowedTarget {
					_, _ = io.WriteString(conn, "HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
					return
				}
				_, _ = io.WriteString(conn, "HTTP/1.1 200 Connection established\r\n\r\n")
				_, _ = io.Copy(conn, br)
			}()
		}
	}()
	return ln.Addr().String(), seen
}

func TestNonLoopbackGoesThroughTheEgressProxy(t *testing.T) {
	proxy, seen := fakeProxy(t, "allowed.example:443")
	relay := startRelay(t, proxy)

	conn, code := socksConnect(t, relay, "allowed.example", 443)
	if code != socksSucceeded {
		t.Fatalf("allowed host: SOCKS reply %d, want success", code)
	}
	if got := <-seen; got != "CONNECT allowed.example:443 browser" {
		t.Fatalf("proxy saw %q", got)
	}
	assertEcho(t, conn, "through the proxy")
	conn.Close()

	conn, code = socksConnect(t, relay, "denied.example", 443)
	defer conn.Close()
	if code != socksNotAllowed {
		t.Fatalf("denied host: SOCKS reply %d, want %d (not allowed)", code, socksNotAllowed)
	}
}

func TestLoopbackStaysInTheSandboxWithAnEgressProxy(t *testing.T) {
	proxy, seen := fakeProxy(t, "allowed.example:443")
	relay := startRelay(t, proxy)
	echo := echoServer(t, "tcp", "127.0.0.1:0")
	conn, code := socksConnect(t, relay, "localhost", portOf(echo))
	defer conn.Close()
	if code != socksSucceeded {
		t.Fatalf("SOCKS reply %d, want success", code)
	}
	assertEcho(t, conn, "local")
	select {
	case got := <-seen:
		t.Fatalf("loopback traffic reached the proxy: %q", got)
	default:
	}
}

func TestConnectPipesStdioToThePort(t *testing.T) {
	echo := echoServer(t, "tcp", "127.0.0.1:0")
	in := strings.NewReader("over stdio")
	var out bytes.Buffer
	if err := runConnect(portOf(echo), in, &out); err != nil {
		t.Fatal(err)
	}
	if out.String() != "over stdio" {
		t.Fatalf("got %q", out.String())
	}
}
