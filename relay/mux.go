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
	frameWindow   byte = 7

	headerSize   = 9
	maxPayload   = 32 * 1024
	streamWindow = 256 * 1024
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

func creditPayload(n int) []byte {
	return binary.BigEndian.AppendUint32(nil, uint32(n))
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

// A stream sends at most streamWindow bytes its peer has not yet written to its conn, and the peer hands that
// credit back (frameWindow) as it writes, so the demux goroutine never waits on a slow conn.
type stream struct {
	id   uint32
	conn net.Conn
	m    *mux

	resetOnce sync.Once

	mu         sync.Mutex
	changed    *sync.Cond
	queue      [][]byte
	queued     int
	window     int
	peerClosed bool
	readDone   bool
	writeDone  bool
	gone       bool
}

// register makes the stream addressable so frames for it queue up; start begins moving bytes.
func (m *mux) register(id uint32, conn net.Conn) *stream {
	s := &stream{id: id, conn: conn, m: m, window: streamWindow}
	s.changed = sync.NewCond(&s.mu)
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
		room := s.awaitWindow()
		if room == 0 {
			return
		}
		n, err := s.conn.Read(buf[:room])
		if n > 0 {
			s.mu.Lock()
			s.window -= n
			s.mu.Unlock()
			if sendErr := s.m.send(frame{kind: frameData, stream: s.id, payload: buf[:n]}); sendErr != nil {
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

// awaitWindow waits until the peer has room for more of this stream's bytes and returns how many to read; 0 once
// the stream is gone.
func (s *stream) awaitWindow() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	for s.window <= 0 && !s.gone {
		s.changed.Wait()
	}
	if s.gone {
		return 0
	}
	return min(s.window, maxPayload)
}

func (s *stream) writeLoop() {
	for {
		chunk, ok := s.next()
		if !ok {
			return
		}
		if chunk == nil {
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
		if err := s.m.send(frame{kind: frameWindow, stream: s.id, payload: creditPayload(len(chunk))}); err != nil {
			s.reset(false)
			return
		}
	}
}

// next returns the next chunk to write to the conn, nil once the peer has closed and every chunk is written, and
// ok false once the stream is gone.
func (s *stream) next() (chunk []byte, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for len(s.queue) == 0 && !s.peerClosed && !s.gone {
		s.changed.Wait()
	}
	if s.gone {
		return nil, false
	}
	if len(s.queue) == 0 {
		return nil, true
	}
	chunk = s.queue[0]
	s.queue[0] = nil
	s.queue = s.queue[1:]
	s.queued -= len(chunk)
	return chunk, true
}

// deliver queues a data frame without blocking; a peer that sends past its window is reset.
func (s *stream) deliver(payload []byte) {
	s.mu.Lock()
	overrun := false
	if !s.gone && !s.peerClosed {
		overrun = s.queued+len(payload) > streamWindow
		if !overrun {
			s.queue = append(s.queue, payload)
			s.queued += len(payload)
			s.changed.Broadcast()
		}
	}
	s.mu.Unlock()
	if overrun {
		s.reset(true)
	}
}

func (s *stream) remoteClosed() {
	s.mu.Lock()
	s.peerClosed = true
	s.changed.Broadcast()
	s.mu.Unlock()
}

func (s *stream) grant(credit uint32) {
	s.mu.Lock()
	s.window = min(s.window+int(credit), streamWindow)
	s.changed.Broadcast()
	s.mu.Unlock()
}

func (s *stream) finish(read, write bool) {
	s.mu.Lock()
	s.readDone = s.readDone || read
	s.writeDone = s.writeDone || write
	complete := s.readDone && s.writeDone && !s.gone
	if complete {
		s.gone = true
		s.changed.Broadcast()
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
		s.queue = nil
		s.changed.Broadcast()
		s.mu.Unlock()
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
	case frameWindow:
		if s := m.lookup(f.stream); s != nil && len(f.payload) == 4 {
			s.grant(binary.BigEndian.Uint32(f.payload))
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

// demux reads frames until the pipe ends; control sees each frame first and returns true for one it consumed.
func (m *mux) demux(in io.Reader, control func(frame) bool) error {
	defer m.closeAll()
	for {
		f, err := readFrame(in)
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			return nil
		}
		if err != nil {
			return err
		}
		if !control(f) {
			m.dispatch(f)
		}
	}
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
