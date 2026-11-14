package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
)

const (
	frameOpen     byte = 1
	frameOpenOK   byte = 2
	frameOpenFail byte = 3
	frameData     byte = 4
	frameClose    byte = 5
	frameReset    byte = 6

	headerSize      = 9
	maxPayload      = 32 * 1024
	streamQueueSize = 64
)

type frame struct {
	kind    byte
	stream  uint32
	payload []byte
}

func writeFrame(w io.Writer, f frame) error {
	if len(f.payload) > maxPayload {
		return fmt.Errorf("frame payload of %d bytes exceeds %d", len(f.payload), maxPayload)
	}
	buf := make([]byte, headerSize+len(f.payload))
	buf[0] = f.kind
	binary.BigEndian.PutUint32(buf[1:5], f.stream)
	binary.BigEndian.PutUint32(buf[5:9], uint32(len(f.payload)))
	copy(buf[headerSize:], f.payload)
	_, err := w.Write(buf)
	return err
}

func readFrame(r io.Reader) (frame, error) {
	var header [headerSize]byte
	if _, err := io.ReadFull(r, header[:]); err != nil {
		return frame{}, err
	}
	size := binary.BigEndian.Uint32(header[5:9])
	if size > maxPayload {
		return frame{}, fmt.Errorf("frame payload of %d bytes exceeds %d", size, maxPayload)
	}
	f := frame{kind: header[0], stream: binary.BigEndian.Uint32(header[1:5])}
	if size > 0 {
		f.payload = make([]byte, size)
		if _, err := io.ReadFull(r, f.payload); err != nil {
			return frame{}, err
		}
	}
	return f, nil
}

func portPayload(port uint16) []byte {
	b := make([]byte, 2)
	binary.BigEndian.PutUint16(b, port)
	return b
}

// mux carries many TCP streams over one byte pipe; each stream is owned by one conn on this side.
type mux struct {
	out     io.Writer
	writeMu sync.Mutex

	mu      sync.Mutex
	streams map[uint32]*stream
	pending map[uint32]chan bool
	nextID  uint32
}

func newMux(out io.Writer) *mux {
	return &mux{out: out, streams: map[uint32]*stream{}, pending: map[uint32]chan bool{}, nextID: 1}
}

func (m *mux) send(f frame) error {
	m.writeMu.Lock()
	defer m.writeMu.Unlock()
	return writeFrame(m.out, f)
}

type stream struct {
	id   uint32
	conn net.Conn
	m    *mux

	in        chan []byte
	inClosed  bool
	done      chan struct{}
	resetOnce sync.Once

	mu         sync.Mutex
	localDone  bool
	remoteDone bool
	gone       bool
}

// register makes the stream addressable so frames for it queue up; start begins moving bytes.
func (m *mux) register(id uint32, conn net.Conn) *stream {
	s := &stream{id: id, conn: conn, m: m, in: make(chan []byte, streamQueueSize), done: make(chan struct{})}
	m.mu.Lock()
	m.streams[id] = s
	m.mu.Unlock()
	return s
}

func (s *stream) start() {
	go s.writeLoop()
	go s.readLoop()
}

func (m *mux) lookup(id uint32) *stream {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.streams[id]
}

func (m *mux) forget(id uint32) {
	m.mu.Lock()
	delete(m.streams, id)
	m.mu.Unlock()
}

func (s *stream) readLoop() {
	buf := make([]byte, maxPayload)
	for {
		n, err := s.conn.Read(buf)
		if n > 0 {
			payload := append([]byte(nil), buf[:n]...)
			if sendErr := s.m.send(frame{kind: frameData, stream: s.id, payload: payload}); sendErr != nil {
				s.reset(false)
				return
			}
		}
		if errors.Is(err, io.EOF) {
			_ = s.m.send(frame{kind: frameClose, stream: s.id})
			s.finish(true, false)
			return
		}
		if err != nil {
			s.reset(true)
			return
		}
	}
}

func (s *stream) writeLoop() {
	for {
		select {
		case chunk, ok := <-s.in:
			if !ok {
				if cw, isTCP := s.conn.(interface{ CloseWrite() error }); isTCP {
					_ = cw.CloseWrite()
				}
				s.finish(false, true)
				return
			}
			if _, err := s.conn.Write(chunk); err != nil {
				s.reset(true)
				return
			}
		case <-s.done:
			return
		}
	}
}

// deliver and remoteClosed run only on the demux goroutine, which is the sole owner of s.in.
func (s *stream) deliver(payload []byte) {
	if s.inClosed {
		return
	}
	select {
	case s.in <- payload:
	case <-s.done:
	}
}

func (s *stream) remoteClosed() {
	if !s.inClosed {
		s.inClosed = true
		close(s.in)
	}
}

func (s *stream) finish(local, remote bool) {
	s.mu.Lock()
	s.localDone = s.localDone || local
	s.remoteDone = s.remoteDone || remote
	complete := s.localDone && s.remoteDone && !s.gone
	if complete {
		s.gone = true
	}
	s.mu.Unlock()
	if complete {
		_ = s.conn.Close()
		s.m.forget(s.id)
	}
}

func (s *stream) reset(notify bool) {
	s.resetOnce.Do(func() {
		s.mu.Lock()
		s.gone = true
		s.mu.Unlock()
		close(s.done)
		_ = s.conn.Close()
		s.m.forget(s.id)
		if notify {
			_ = s.m.send(frame{kind: frameReset, stream: s.id})
		}
	})
}

// dispatch routes a data-plane frame to its stream; it reports whether the frame was one.
func (m *mux) dispatch(f frame) bool {
	switch f.kind {
	case frameData:
		if s := m.lookup(f.stream); s != nil {
			s.deliver(f.payload)
		}
	case frameClose:
		if s := m.lookup(f.stream); s != nil {
			s.remoteClosed()
		}
	case frameReset:
		if s := m.lookup(f.stream); s != nil {
			s.reset(false)
		}
	default:
		return false
	}
	return true
}

func (m *mux) closeAll() {
	m.mu.Lock()
	streams := make([]*stream, 0, len(m.streams))
	for _, s := range m.streams {
		streams = append(streams, s)
	}
	for id, ch := range m.pending {
		close(ch)
		delete(m.pending, id)
	}
	m.mu.Unlock()
	for _, s := range streams {
		s.reset(false)
	}
}
