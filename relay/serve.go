package main

import (
	"encoding/binary"
	"errors"
	"io"
	"net"
	"strconv"
	"time"
)

const dialTimeout = 5 * time.Second

type server struct {
	m *mux
}

func newServer(m *mux) *server {
	return &server{m: m}
}

func (s *server) demux(in io.Reader) error {
	defer s.m.closeAll()
	for {
		f, err := readFrame(in)
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			return nil
		}
		if err != nil {
			return err
		}
		if s.m.dispatch(f) {
			continue
		}
		if f.kind == frameOpen && len(f.payload) == 2 {
			go s.open(f.stream, binary.BigEndian.Uint16(f.payload))
		}
	}
}

func (s *server) open(id uint32, port uint16) {
	conn, err := dialLoopback(port)
	if err != nil {
		_ = s.m.send(frame{kind: frameOpenFail, stream: id})
		return
	}
	st := s.m.register(id, conn)
	if err := s.m.send(frame{kind: frameOpenOK, stream: id}); err != nil {
		st.reset(false)
		return
	}
	st.start()
}

// dialLoopback reaches a service bound to either loopback family, as dev servers bind one or the other.
func dialLoopback(port uint16) (net.Conn, error) {
	p := strconv.Itoa(int(port))
	conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", p), dialTimeout)
	if err == nil {
		return conn, nil
	}
	conn, err6 := net.DialTimeout("tcp", net.JoinHostPort("::1", p), dialTimeout)
	if err6 == nil {
		return conn, nil
	}
	return nil, err
}
