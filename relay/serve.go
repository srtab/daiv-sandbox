package main

import (
	"encoding/binary"
	"io"
	"net"
	"strconv"
	"sync"
	"time"
)

const dialTimeout = 5 * time.Second

type server struct {
	m    *mux
	dial func(port uint16) (net.Conn, error)

	mu sync.Mutex
	// Streams still dialing, and whether the peer reset them meanwhile.
	dialing map[uint32]bool
}

func newServer(m *mux) *server {
	return &server{m: m, dial: dialLoopback, dialing: map[uint32]bool{}}
}

func (s *server) demux(in io.Reader) error {
	return s.m.demux(in, s.control)
}

// control records a reset of a stream still dialing (then lets the mux reset it too) and starts each open.
func (s *server) control(f frame) bool {
	switch f.kind {
	case frameReset:
		s.mu.Lock()
		if _, ok := s.dialing[f.stream]; ok {
			s.dialing[f.stream] = true
		}
		s.mu.Unlock()
		return false
	case frameOpen:
		if len(f.payload) == 2 {
			s.mu.Lock()
			s.dialing[f.stream] = false
			s.mu.Unlock()
			go s.open(f.stream, binary.BigEndian.Uint16(f.payload))
		}
		return true
	}
	return false
}

func (s *server) open(id uint32, port uint16) {
	conn, err := s.dial(port)
	s.mu.Lock()
	reset := s.dialing[id]
	delete(s.dialing, id)
	var st *stream
	if err == nil && !reset {
		st = s.m.register(id, conn)
	}
	s.mu.Unlock()
	if err != nil {
		if !reset {
			_ = s.m.send(frame{kind: frameOpenFail, stream: id})
		}
		return
	}
	if reset {
		_ = conn.Close()
		return
	}
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
