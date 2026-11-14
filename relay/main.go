// Command daiv-relay carries a headless browser's traffic for daiv-sandbox.
//
//	daiv-relay listen [--addr 127.0.0.1:39223] [--egress-proxy host:port]
//	daiv-relay serve
//	daiv-relay connect <port>
package main

import (
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"time"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	var err error
	switch os.Args[1] {
	case "listen":
		fs := flag.NewFlagSet("listen", flag.ExitOnError)
		addr := fs.String("addr", "127.0.0.1:39223", "SOCKS5 listen address")
		egress := fs.String("egress-proxy", "", "HTTP CONNECT proxy for non-loopback destinations")
		_ = fs.Parse(os.Args[2:])
		err = runListen(*addr, *egress, os.Stdin, os.Stdout)
	case "serve":
		err = runServe(os.Stdin, os.Stdout)
	case "connect":
		if len(os.Args) != 3 {
			usage()
		}
		port, perr := strconv.ParseUint(os.Args[2], 10, 16)
		if perr != nil {
			usage()
		}
		err = runConnect(uint16(port), os.Stdin, os.Stdout)
	default:
		usage()
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "daiv-relay:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: daiv-relay listen [--addr host:port] [--egress-proxy host:port] | serve | connect <port>")
	os.Exit(2)
}

func runListen(addr, egressProxy string, in io.Reader, out io.Writer) error {
	ln, err := listenWithRetry(addr, 5*time.Second)
	if err != nil {
		return err
	}
	defer ln.Close()
	l := newListener(newMux(out), egressProxy)
	go l.accept(ln)
	return l.demux(in)
}

// listenWithRetry waits out a previous relay that has not released the port yet.
func listenWithRetry(addr string, wait time.Duration) (net.Listener, error) {
	deadline := time.Now().Add(wait)
	for {
		ln, err := net.Listen("tcp", addr)
		if err == nil || time.Now().After(deadline) {
			return ln, err
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func runServe(in io.Reader, out io.Writer) error {
	return newServer(newMux(out)).demux(in)
}

func runConnect(port uint16, in io.Reader, out io.Writer) error {
	conn, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(int(port))))
	if err != nil {
		return err
	}
	defer conn.Close()
	go func() {
		_, _ = io.Copy(conn, in)
		if tcp, ok := conn.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
	}()
	_, err = io.Copy(out, conn)
	return err
}
