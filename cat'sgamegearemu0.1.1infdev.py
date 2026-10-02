#!/usr/bin/env python3
"""Blue Gear: experimental Sega Game Gear native-mode emulator, Python 3.10+/Tk.
Run: python3 blue_gamegear.py | python3 blue_gamegear.py --self-test
Original machine-code demo included; no Sega firmware or commercial ROMs.
No settings, saves, ROMs, or other app files are written by this program.
Adapted from the generated Blue SMS and SG-1000 siblings; all code is embedded.
Optional installed pygame or ffplay enables memory-only stereo playback.
Hardware implementation and demo are original; see README for limits/sources.
"""
from __future__ import annotations
import argparse
import os
import time

# The repeating block-I/O flag equations were adapted with reference to
# SingleStepTests/z80 generation/z80_test_generator.js. That reference is MIT:
# Copyright (c) 2024 SingleStepTests
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

S, Z, Y, H, X, P, N, C = 128, 64, 32, 16, 8, 4, 2, 1
SZ53 = tuple((v & 0xA8) | (Z if v == 0 else 0) for v in range(256))
SZ53P = tuple(SZ53[v] | (P if v.bit_count() % 2 == 0 else 0) for v in range(256))


class Z80:
    """Instruction-level NMOS Z80. T-states, not bus-cycle contention."""
    def __init__(self, bus):
        self.bus = bus
        self.m = bus.memory
        self.reset()

    def reset(self):
        for name in ('a','f','b','c','d','e','h','l','i','r','ix','iy','pc',
                     'af_','bc_','de_','hl_','im','wz','q','ei'):
            setattr(self, name, 0)
        self.sp = 0xFFFF
        self.iff1 = self.iff2 = False
        self.halted = False
        self.tstates = 0

    def read16(self, address):
        return self.m[address & 65535] | self.m[(address + 1) & 65535] << 8

    def write16(self, address, value):
        self.bus.write(address, value & 255)
        self.bus.write(address + 1, (value >> 8) & 255)

    def fetch(self):
        value = self.m[self.pc]
        self.pc = (self.pc + 1) & 65535
        return value

    def fetch16(self):
        return self.fetch() | self.fetch() << 8

    def opcode(self):
        self.r = (self.r & 128) | ((self.r + 1) & 127)
        return self.fetch()

    def push(self, value):
        self.sp = (self.sp - 2) & 65535
        self.write16(self.sp, value)

    def pop(self):
        value = self.read16(self.sp)
        self.sp = (self.sp + 2) & 65535
        return value

    def pair(self, p, index=None, af=False):
        if p == 0: return self.b << 8 | self.c
        if p == 1: return self.d << 8 | self.e
        if p == 2: return getattr(self, index) if index else self.h << 8 | self.l
        return self.a << 8 | self.f if af else self.sp

    def setpair(self, p, value, index=None, af=False):
        value &= 65535
        hi, lo = value >> 8, value & 255
        if p == 0: self.b, self.c = hi, lo
        elif p == 1: self.d, self.e = hi, lo
        elif p == 2:
            if index: setattr(self, index, value)
            else: self.h, self.l = hi, lo
        elif af: self.a, self.f = hi, lo
        else: self.sp = value

    def reg(self, n, index=None, address=None):
        if n == 6: return self.m[self.pair(2) if address is None else address]
        if index and n in (4, 5):
            return (getattr(self, index) >> (8 if n == 4 else 0)) & 255
        return getattr(self, ('b','c','d','e','h','l','_','a')[n])

    def setreg(self, n, value, index=None, address=None):
        value &= 255
        if n == 6: self.bus.write(self.pair(2) if address is None else address, value)
        elif index and n in (4, 5):
            old = getattr(self, index)
            setattr(self, index, ((value << 8) | (old & 255)) if n == 4 else ((old & 65280) | value))
        else: setattr(self, ('b','c','d','e','h','l','_','a')[n], value)

    def condition(self, y):
        return ((not self.f & Z), bool(self.f & Z), (not self.f & C), bool(self.f & C),
                (not self.f & P), bool(self.f & P), (not self.f & S), bool(self.f & S))[y]

    def alu(self, op, value):
        a = self.a
        carry = (self.f & C) if op in (1, 3) else 0
        if op in (0, 1):
            wide = a + value + carry
            r = wide & 255
            self.f = SZ53[r] | ((a ^ value ^ r) & H) | (P if (~(a ^ value) & (a ^ r) & 128) else 0) | (C if wide > 255 else 0)
            self.a = r
        elif op in (2, 3, 7):
            wide = a - value - carry
            r = wide & 255
            self.f = SZ53[r] | N | ((a ^ value ^ r) & H) | (P if ((a ^ value) & (a ^ r) & 128) else 0) | (C if wide < 0 else 0)
            if op == 7: self.f = (self.f & ~0x28) | (value & 0x28)
            else: self.a = r
        else:
            self.a = a & value if op == 4 else a ^ value if op == 5 else a | value
            self.f = SZ53P[self.a] | (H if op == 4 else 0)

    def incdec(self, value, decrement):
        r = (value + (-1 if decrement else 1)) & 255
        self.f = (self.f & C) | SZ53[r] | ((value ^ r) & H) | (N if decrement else 0) | (P if value == (128 if decrement else 127) else 0)
        return r

    def relative(self):
        d = self.fetch()
        return d - 256 if d & 128 else d

    def step(self):
        if self.ei: self.ei -= 1
        old_q = self.q
        self.q = 0
        if self.halted:
            self.r = (self.r & 128) | ((self.r + 1) & 127)
            self.tstates += 4
            return 4
        op = self.opcode()
        prefix = 0
        index = None
        # Repeated index prefixes each consume time, with the last one winning.
        while op in (0xDD, 0xFD):
            prefix += 4
            index = 'ix' if op == 0xDD else 'iy'
            if prefix >= 65536 * 4:
                raise RuntimeError('An entire address space of index prefixes cannot complete an instruction')
            op = self.opcode()
        if op == 0xCB:
            if index:
                address = (getattr(self, index) + self.relative()) & 65535
                self.wz = address
                op = self.fetch()  # DDCB final byte is not an M1 fetch.
                cycles = self.cb(op, address, True) + prefix
            else:
                cycles = self.cb(self.opcode(), self.pair(2), False)
        elif op == 0xED:
            cycles = self.ed(self.opcode()) + prefix
        else:
            cycles = self.base(op, index, old_q) + prefix
        # Q is set when an instruction writes flags, even if their value stays the same.
        if self._flags_written(op, index):
            self.q = self.f
        self.tstates += cycles
        return cycles

    def _flags_written(self, op, index):
        # Dispatchers explicitly set this latch. Kept separate from flag equality.
        result = getattr(self, '_flag_write', False)
        self._flag_write = False
        return result

    def base(self, op, index, old_q):
        x, y, z = op >> 6, (op >> 3) & 7, op & 7
        p, q = y >> 1, y & 1
        address = None
        memory = ((x == 0 and z in (4,5,6) and y == 6) or
                  (x == 1 and op != 0x76 and (y == 6 or z == 6)) or
                  (x == 2 and z == 6))
        if index and memory:
            address = (getattr(self, index) + self.relative()) & 65535
            self.wz = address
        if x == 0:
            if z == 0:
                if y == 0: return 4
                if y == 1:
                    v = self.a << 8 | self.f
                    self.a, self.f = self.af_ >> 8, self.af_ & 255
                    self.af_ = v
                    return 4
                displacement = self.relative()
                if y == 2:
                    self.b = (self.b - 1) & 255
                    take = self.b != 0
                else: take = y == 3 or self.condition(y - 4)
                if take:
                    self.pc = (self.pc + displacement) & 65535
                    self.wz = self.pc
                return (13 if take else 8) if y == 2 else (12 if take else 7)
            if z == 1:
                if not q:
                    self.setpair(p, self.fetch16(), index)
                    return 10
                a, b = self.pair(2,index), self.pair(p,index)
                wide = a + b
                self.wz = (a + 1) & 65535
                self.f = (self.f & (S|Z|P)) | ((wide >> 8) & 0x28) | (H if (a ^ b ^ wide) & 4096 else 0) | (C if wide > 65535 else 0)
                self.setpair(2, wide, index)
                self._flag_write = True
                return 11
            if z == 2:
                if p < 2:
                    addr = self.pair(p)
                    if q:
                        self.a = self.m[addr]
                        self.wz = (addr + 1) & 65535
                    else:
                        self.bus.write(addr,self.a)
                        self.wz = self.a << 8 | ((addr + 1) & 255)
                    return 7
                addr = self.fetch16()
                if p == 2:
                    if q: self.setpair(2,self.read16(addr),index)
                    else: self.write16(addr,self.pair(2,index))
                    self.wz = (addr + 1) & 65535
                    return 16
                if q:
                    self.a = self.m[addr]
                    self.wz = (addr + 1) & 65535
                else:
                    self.bus.write(addr,self.a)
                    self.wz = self.a << 8 | ((addr + 1) & 255)
                return 13
            if z == 3:
                self.setpair(p,self.pair(p,index) + (-1 if q else 1),index)
                return 6
            if z in (4,5):
                self.setreg(y,self.incdec(self.reg(y,index,address),z == 5),index,address)
                self._flag_write = True
                return (19 if index else 11) if y == 6 else 4
            if z == 6:
                self.setreg(y,self.fetch(),index,address)
                return (15 if index else 10) if y == 6 else 7
            a, f = self.a, self.f
            self._flag_write = True
            if y < 4:
                if y == 0: self.a, carry = ((a << 1) | (a >> 7)) & 255, a >> 7
                elif y == 1: self.a, carry = (a >> 1) | ((a & 1) << 7), a & 1
                elif y == 2: self.a, carry = ((a << 1) | (f & C)) & 255, a >> 7
                else: self.a, carry = (a >> 1) | ((f & C) << 7), a & 1
                self.f = (f & (S|Z|P)) | (self.a & 0x28) | carry
            elif y == 4:
                correction = 0
                carry = f & C
                if f & H or (a & 15) > 9: correction |= 6
                if carry or a > 0x99: correction |= 0x60; carry = C
                self.a = (a - correction if f & N else a + correction) & 255
                self.f = SZ53P[self.a] | (f & N) | carry | ((a ^ self.a) & H)
            elif y == 5:
                self.a ^= 255
                self.f = (f & (S|Z|P|C)) | H | N | (self.a & 0x28)
            elif y == 6:
                self.f = (f & (S|Z|P)) | (((f ^ old_q) | a) & 0x28) | C
            else:
                self.f = (f & (S|Z|P)) | (((f ^ old_q) | a) & 0x28) | (H if f & C else C)
            return 4
        if x == 1:
            if op == 0x76:
                self.halted = True
                return 4
            # LD H,(IX+d) / LD (IX+d),H use real H/L, not index halves.
            reg_index = None if memory else index
            self.setreg(y,self.reg(z,reg_index,address),reg_index,address)
            return (15 if index else 7) if memory else 4
        if x == 2:
            self.alu(y,self.reg(z,index,address))
            self._flag_write = True
            return (15 if index else 7) if z == 6 else 4
        if z == 0:
            if self.condition(y):
                self.pc = self.pop(); self.wz = self.pc
                return 11
            return 5
        if z == 1:
            if not q:
                self.setpair(p,self.pop(),index,True)
                return 10
            if p == 0:
                self.pc = self.pop(); self.wz = self.pc
                return 10
            if p == 1:
                for n in range(3):
                    old = self.pair(n)
                    attr = ('bc_','de_','hl_')[n]
                    self.setpair(n,getattr(self,attr))
                    setattr(self,attr,old)
                return 4
            if p == 2: self.pc = self.pair(2,index); return 4
            self.sp = self.pair(2,index)
            return 6
        if z == 2:
            addr = self.fetch16(); self.wz = addr
            if self.condition(y): self.pc = addr
            return 10
        if z == 3:
            if y == 0: self.pc = self.fetch16(); self.wz = self.pc; return 10
            if y in (2,3):
                port = self.a << 8 | self.fetch()
                if y == 2:
                    self.bus.out_port(port,self.a)
                    self.wz = (port & 65280) | ((port + 1) & 255)
                else:
                    self.a = self.bus.in_port(port)
                    self.wz = (port + 1) & 65535
                return 11
            if y == 4:
                value = self.read16(self.sp)
                self.write16(self.sp,self.pair(2,index))
                self.setpair(2,value,index); self.wz = value
                return 19
            if y == 5:
                de = self.pair(1); self.setpair(1,self.pair(2)); self.setpair(2,de)
                return 4
            if y == 6:
                self.iff1 = self.iff2 = False; self.ei = 0
                return 4
            if y == 7:
                self.iff1 = self.iff2 = True; self.ei = 2
                return 4
        if z == 4:
            addr = self.fetch16(); self.wz = addr
            if self.condition(y): self.push(self.pc); self.pc = addr; return 17
            return 10
        if z == 5:
            if not q: self.push(self.pair(p,index,True)); return 11
            if p == 0:
                addr = self.fetch16(); self.push(self.pc); self.pc = addr; self.wz = addr
                return 17
        if z == 6:
            self.alu(y,self.fetch()); self._flag_write = True
            return 7
        if z == 7:
            self.push(self.pc); self.pc = y * 8; self.wz = self.pc
            return 11
        raise RuntimeError('Internal opcode dispatch error: %02X' % op)

    def cb(self, op, address, indexed):
        x,y,z = op >> 6, (op >> 3) & 7, op & 7
        value = self.m[address] if indexed else self.reg(z)
        result = value
        if x == 0:
            if y == 0: result, carry = ((value << 1) | (value >> 7)) & 255, value >> 7
            elif y == 1: result, carry = (value >> 1) | ((value & 1) << 7), value & 1
            elif y == 2: result, carry = ((value << 1) | (self.f & C)) & 255, value >> 7
            elif y == 3: result, carry = (value >> 1) | ((self.f & C) << 7), value & 1
            elif y == 4: result, carry = (value << 1) & 255, value >> 7
            elif y == 5: result, carry = (value >> 1) | (value & 128), value & 1
            elif y == 6: result, carry = ((value << 1) | 1) & 255, value >> 7
            else: result, carry = value >> 1, value & 1
            self.f = SZ53P[result] | carry
            self._flag_write = True
        elif x == 1:
            bits = (address >> 8) if indexed else (self.wz >> 8) if z == 6 else value
            self.f = (self.f & C) | H | (bits & 0x28) | (0 if value & (1 << y) else Z|P) | (S if y == 7 and value & 128 else 0)
            self._flag_write = True
        elif x == 2: result = value & ~(1 << y)
        else: result = value | (1 << y)
        if x != 1:
            if indexed:
                self.bus.write(address,result)
                if z != 6: self.setreg(z,result)
            else: self.setreg(z,result)
        return (16 if x == 1 else 19) if indexed else (12 if x == 1 else 15) if z == 6 else 8

    def ed(self, op):
        x,y,z = op >> 6, (op >> 3) & 7, op & 7
        p,q = y >> 1, y & 1
        if x == 1:
            if z == 0:
                port = self.pair(0); value = self.bus.in_port(port)
                if y != 6: self.setreg(y,value)
                self.f = (self.f & C) | SZ53P[value]; self.wz = (port + 1) & 65535
                self._flag_write = True
                return 12
            if z == 1:
                port = self.pair(0)
                self.bus.out_port(port,self.reg(y) if y != 6 else 0)
                self.wz = (port + 1) & 65535
                return 12
            if z == 2:
                a,b,carry = self.pair(2),self.pair(p),self.f & C
                wide = a + b + carry if q else a - b - carry
                r = wide & 65535
                overflow = (~(a ^ b) & (a ^ r)) if q else ((a ^ b) & (a ^ r))
                self.f = ((r >> 8) & 0xA8) | (Z if r == 0 else 0) | (H if (a ^ b ^ r) & 4096 else 0) | (P if overflow & 32768 else 0) | (0 if q else N) | (C if wide < 0 or wide > 65535 else 0)
                self.wz = (a + 1) & 65535; self.setpair(2,r)
                self._flag_write = True
                return 15
            if z == 3:
                addr = self.fetch16()
                if q: self.setpair(p,self.read16(addr))
                else: self.write16(addr,self.pair(p))
                self.wz = (addr + 1) & 65535
                return 20
            if z == 4:
                value = self.a; self.a = 0; self.alu(2,value)
                self._flag_write = True
                return 8
            if z == 5:
                self.pc = self.pop(); self.wz = self.pc; self.iff1 = self.iff2
                return 14
            if z == 6:
                self.im = (0,0,1,2,0,0,1,2)[y]
                return 8
            if z == 7:
                if y == 0: self.i = self.a; return 9
                if y == 1: self.r = self.a; return 9
                if y in (2,3):
                    self.a = self.i if y == 2 else self.r
                    self.f = (self.f & C) | SZ53[self.a] | (P if self.iff2 else 0)
                    self._flag_write = True
                    return 9
                if y in (4,5):
                    addr = self.pair(2); value = self.m[addr]
                    if y == 4:
                        self.bus.write(addr,((self.a & 15) << 4) | (value >> 4))
                        self.a = (self.a & 240) | (value & 15)
                    else:
                        self.bus.write(addr,((value << 4) & 255) | (self.a & 15))
                        self.a = (self.a & 240) | (value >> 4)
                    self.f = (self.f & C) | SZ53P[self.a]; self.wz = (addr + 1) & 65535
                    self._flag_write = True
                    return 18
        if x == 2 and y >= 4 and z <= 3:
            direction = -1 if y & 1 else 1
            repeat = y >= 6
            hl,bc = self.pair(2),self.pair(0)
            if z == 0:
                value = self.m[hl]; self.bus.write(self.pair(1),value)
                self.setpair(2,hl + direction); self.setpair(1,self.pair(1) + direction); self.setpair(0,bc - 1)
                n = (self.a + value) & 255
                self.f = (self.f & (S|Z|C)) | (P if self.pair(0) else 0) | (n & X) | ((n & 2) << 4)
                again = self.pair(0) != 0
            elif z == 1:
                value = self.m[hl]; result = (self.a - value) & 255
                half = (self.a ^ value ^ result) & H
                self.setpair(2,hl + direction); self.setpair(0,bc - 1)
                n = (result - (1 if half else 0)) & 255
                self.f = (self.f & C) | (SZ53[result] & (S|Z)) | N | half | (P if self.pair(0) else 0) | (n & X) | ((n & 2) << 4)
                self.wz = (self.wz + direction) & 65535
                again = self.pair(0) != 0 and result != 0
            elif z == 2:
                value = self.bus.in_port(bc); self.bus.write(hl,value)
                self.setpair(2,hl + direction); self.b = (self.b - 1) & 255
                total = value + ((self.c + direction) & 255)
                self.f = SZ53[self.b] | (N if value & 128 else 0) | (H|C if total > 255 else 0) | (SZ53P[((total & 7) ^ self.b)] & P)
                self.wz = (bc + direction) & 65535
                again = self.b != 0
            else:
                value = self.m[hl]; self.setpair(2,hl + direction); self.b = (self.b - 1) & 255
                self.bus.out_port(self.pair(0),value)
                total = value + self.l
                self.f = SZ53[self.b] | (N if value & 128 else 0) | (H|C if total > 255 else 0) | (SZ53P[((total & 7) ^ self.b)] & P)
                self.wz = (self.pair(0) + direction) & 65535
                again = self.b != 0
            self._flag_write = True
            if repeat and again:
                self.pc = (self.pc - 2) & 65535
                self.wz = (self.pc + 1) & 65535
                self.f = (self.f & ~0x28) | ((self.pc >> 8) & 0x28)
                if z >= 2:
                    # Repeating block-I/O flags follow the NMOS repeat-path
                    # behavior verified by SingleStepTests (MIT, notice below).
                    parity_operand = self.b & 7
                    if self.f & C:
                        negative = bool(value & 128)
                        parity_operand = (self.b + (-1 if negative else 1)) & 7
                        half = (self.b & 15) == (0 if negative else 15)
                        self.f = (self.f & ~H) | (H if half else 0)
                    self.f ^= (SZ53P[parity_operand] & P) ^ P
                return 21
            return 16
        return 8  # Unassigned ED encodings are two-byte NOPs on NMOS Z80.

    def interrupt(self):
        if not self.iff1 or self.ei > 1: return 0
        self.halted = False
        self.iff1 = self.iff2 = False
        self.r = (self.r & 128) | ((self.r + 1) & 127)
        self.push(self.pc)
        self.pc = self.read16((self.i << 8) | 255) if self.im == 2 else 0x38
        self.wz = self.pc
        self.q = 0
        cycles = 19 if self.im == 2 else 13
        self.tstates += cycles
        return cycles


class VDP:
    """GG Mode 4: 256x192 internal raster, 160x144 LCD and latched 12-bit CRAM.
    Scanline timing; extended-height and SMS-compatibility modes are omitted.
    """
    def __init__(self):
        self.vram = bytearray(16384); self.cram = bytearray(64); self.cram_latch = 0
        self.reg = bytearray(16); self.address = 0; self.code = 0
        self.latch = None; self.buffer = 0; self.status = 0
        self.line = 0; self.cycles = 0; self.frames = 0
        self.line_counter = 0; self.line_pending = False; self.frame_pending = False
        self.yscroll = 0; self.pattern_cache = {}; self.palette = [bytes(3)]*32
        self.rgb = bytearray(256*192*3)
        self.front = bytes(160*144*3)

    @property
    def irq(self):
        return ((self.frame_pending and bool(self.reg[1]&32)) or
                (self.line_pending and bool(self.reg[0]&16)))

    @property
    def supported(self):
        return bool(self.reg[0]&4) and not (self.reg[0]&2 and (self.reg[1]&24) in (8,16)) and not (not self.reg[0]&2 and self.reg[1]&16)

    def control(self, value):
        if self.latch is None:
            self.latch = value; self.address = (self.address&0x3F00)|value; return
        self.address = ((value&63)<<8) | self.latch; self.code = value>>6
        if self.code == 2:
            register = value&15
            if register < 11: self.reg[register] = self.latch
        elif self.code == 0:
            self.buffer = self.vram[self.address]
            self.address = (self.address+1)&16383
        self.latch = None

    def write_data(self, value):
        self.latch = None; self.buffer = value
        if self.code == 3:
            index = self.address&63
            if not index&1:
                self.cram_latch = value
            else:
                # Even writes only latch. The next odd write commits both bytes,
                # even when its address is not adjacent to that even write.
                low = self.cram_latch; high = value&15
                self.cram[index-1:index+1] = bytes((low, high))
                self.palette[index>>1] = bytes(((low&15)*17,(low>>4)*17,high*17))
        else:
            self.vram[self.address] = value
            self.pattern_cache.pop(self.address&~3, None)
        self.address = (self.address+1)&16383

    def read_data(self):
        self.latch = None; value = self.buffer
        self.buffer = self.vram[self.address]; self.address=(self.address+1)&16383
        return value

    def read_status(self):
        value = self.status; self.status = 0; self.latch = None
        self.frame_pending = self.line_pending = False
        return value

    def pattern(self, tile, row):
        address = ((tile&511)*32+(row&7)*4)&16383
        result = self.pattern_cache.get(address)
        if result is None:
            a,b,c,d = self.vram[address:address+4]
            result = tuple(((a>>bit)&1)|(((b>>bit)&1)<<1)|(((c>>bit)&1)<<2)|(((d>>bit)&1)<<3) for bit in range(7,-1,-1))
            self.pattern_cache[address] = result
        return result

    def render_line(self, y):
        r = self.reg; backdrop = 16+(r[7]&15)
        if not r[1]&64 or not self.supported:
            self.rgb[y*768:(y+1)*768] = self.palette[backdrop]*256; return
        v = self.vram; name = (r[2]&14)<<10
        hs = 0 if r[0]&64 and y<16 else r[8]
        lock_edge = 192+(hs&7)
        pixels = [0]*256; priority = [False]*256
        # Run in spans, splitting at both tile and vertical-scroll-lock edges.
        x = 0
        while x<256:
            sx = (x-hs)&255
            sy = y if r[0]&128 and x>=lock_edge else (y+self.yscroll)%224
            address = (name+(sy>>3)*64+(sx>>3)*2)&16383
            attr = v[address] | (v[(address+1)&16383]<<8)
            row = 7-(sy&7) if attr&1024 else sy&7
            pattern = self.pattern(attr&511,row)
            if attr&512: pattern = pattern[::-1]
            count = min(8-(sx&7),256-x,lock_edge-x if x<lock_edge else 256-x)
            colors = pattern[sx&7:(sx&7)+count]
            bank = 16 if attr&2048 else 0
            pixels[x:x+count] = [c+bank for c in colors]
            if attr&4096: priority[x:x+count] = [bool(c) for c in colors]
            x += count
        if hs&7:
            pixels[:hs&7] = [backdrop]*(hs&7)
            priority[:hs&7] = [False]*(hs&7)
        base = (r[5]&126)<<7; tall = bool(r[1]&2); zoom = 2 if r[1]&1 else 1
        height = (16 if tall else 8)*zoom; occupied = [False]*256; found = 0
        for index in range(64):
            raw_y = v[base+index]
            if raw_y == 208: break
            top = (raw_y+1)&255
            if top>=240: top-=256
            if not top<=y<top+height: continue
            found += 1
            if found>8:
                self.status |= 64; break
            sx = v[base+128+index*2]-(8 if r[0]&8 else 0)
            tile = v[base+129+index*2] | ((r[6]&4)<<6)
            row = (y-top)//zoom
            if tall: tile = (tile&~1)+(row>>3)
            pattern = self.pattern(tile,row&7)
            for offset, color in enumerate(pattern):
                if not color: continue
                for repeat in range(zoom):
                    x = sx+offset*zoom+repeat
                    if 0<=x<256:
                        if occupied[x]: self.status |= 32
                        else:
                            occupied[x] = True
                            if not priority[x]: pixels[x] = color+16
        if r[0]&32: pixels[:8] = [backdrop]*8
        self.rgb[y*768:(y+1)*768] = b''.join(self.palette[c] for c in pixels)

    def tick(self, cycles):
        self.cycles += cycles
        while self.cycles>=228:
            self.cycles-=228
            if self.line<192: self.render_line(self.line)
            if self.line<=192:
                if self.line_counter == 0:
                    self.line_counter = self.reg[10]; self.line_pending = True
                else: self.line_counter-=1
            else: self.line_counter = self.reg[10]
            self.line+=1
            if self.line==193:
                self.status |= 128; self.frame_pending = True
            elif self.line==262:
                self.line = 0; self.frames+=1; self.yscroll = self.reg[9]
                self.front = self.lcd_pixels()

    def lcd_pixels(self):
        # Native GG LCD shows x=48..207, y=24..167, not a scaled SMS frame.
        return b''.join(self.rgb[y*768+144:y*768+624] for y in range(24,168))

    def vcounter(self):
        return self.line if self.line<=218 else self.line-6


class PSG:
    """Approximate Sega integrated PSG with GG headphone stereo routing.
    Tone zero acts as one. Noise uses a 16-bit LFSR with bit 0/3 XOR taps.
    44100 Hz s16le stereo PCM stays in bounded RAM; no analog filtering.
    """
    CLOCK = 3_579_545
    SAMPLE_RATE = 44100
    VOLUME = tuple(round(5000 * 10 ** (-i / 10)) for i in range(15)) + (0,)
    def __init__(self):
        self.reg = [0,15,0,15,0,15,0,15]
        self.latch = 3
        self.counter = [1,1,1,1]
        self.output = [0,0,0,0]
        self.lfsr = 0x8000
        self.divider = self.sample_phase = 0
        self.stereo = 255
        self.pcm = bytearray()
        self.samples = 0

    def write(self, value):
        value &= 255
        if value & 128:
            self.latch = (value >> 4) & 7
            if self.latch in (0,2,4):
                self.reg[self.latch] = (self.reg[self.latch] & 0x3F0) | (value & 15)
            else: self.reg[self.latch] = value & (7 if self.latch == 6 else 15)
        elif self.latch in (0,2,4):
            self.reg[self.latch] = (self.reg[self.latch] & 15) | ((value & 63) << 4)
        else: self.reg[self.latch] = value & (7 if self.latch == 6 else 15)
        if self.latch == 6:
            self.lfsr = 0x8000
            self.output[3] = 0

    def clock_tick(self):
        r = self.reg
        for ch in range(3):
            self.counter[ch] -= 1
            if self.counter[ch] <= 0:
                self.counter[ch] = r[ch*2] or 1
                self.output[ch] ^= 1
        self.counter[3] -= 1
        if self.counter[3] <= 0:
            rate = r[6] & 3
            self.counter[3] = max(1,2*r[4]) if rate == 3 else (32 << rate)
            feedback = (self.lfsr ^ (self.lfsr >> 3 if r[6]&4 else 0)) & 1
            self.lfsr = (self.lfsr >> 1) | (feedback << 15)
            self.output[3] = self.lfsr & 1

    def advance(self, cycles):
        self.divider += cycles
        ticks, self.divider = divmod(self.divider,16)
        for _ in range(ticks):
            self.clock_tick()
            self.sample_phase += self.SAMPLE_RATE * 16
            if self.sample_phase >= self.CLOCK:
                self.sample_phase -= self.CLOCK
                waves = [self.VOLUME[self.reg[ch*2+1]] * (1 if self.output[ch] else -1) for ch in range(4)]
                left = sum(waves[ch] for ch in range(4) if self.stereo&(16<<ch))
                right = sum(waves[ch] for ch in range(4) if self.stereo&(1<<ch))
                self.pcm.extend(int(left).to_bytes(2,'little',signed=True))
                self.pcm.extend(int(right).to_bytes(2,'little',signed=True))
                self.samples += 1
        # Headless use / unavailable playback must never grow memory unbounded.
        limit = self.SAMPLE_RATE  # one quarter second of stereo s16le
        if len(self.pcm) > limit: del self.pcm[:len(self.pcm)-limit]

    def drain(self):
        result = bytes(self.pcm); self.pcm.clear(); return result


class GameGear:
    CLOCK = 3_579_545
    FRAME = 228*262
    def __init__(self, rom=None, title='STAR CHASE / original demo'):
        self.rom = self.normalize_rom(rom) if rom is not None else demo_rom()
        self.title = title; self.demo = rom is None
        self.reset()

    def reset(self):
        self.memory = bytearray(65536); self.sram = bytearray(32768)
        self.mapper = [0,0,1,2]; self.io_control = 255; self.mem_control = 0
        self.keys = set(); self.psg = PSG()
        self.h_latch = 0
        self.gg_ports = bytearray((0,127,255,0,255,0))
        self.vdp = VDP(); self.cpu = Z80(self); self.remap()
        for i,n in enumerate(self.mapper): self.memory[0xDFFC+i] = self.memory[0xFFFC+i] = n

    @staticmethod
    def normalize_rom(data):
        if len(data)%8192 == 512: data=data[512:]
        if len(data)<8192 or len(data)>4*1024*1024 or len(data)%8192:
            raise ValueError('Use an uncompressed 8 KiB-aligned Game Gear ROM (8 KiB to 4 MiB). A 512-byte copier header is allowed.')
        if len(data)==8192: data=data*2
        elif len(data)%16384: data=data+bytes([255])*8192
        return bytes(data)

    def load_rom(self, data, title='Cartridge'):
        rom=self.normalize_rom(data)  # Validate fully before replacing the old machine.
        self.rom=rom; self.title=title; self.demo=False; self.reset()

    def remap(self):
        pages = max(1,len(self.rom)//16384)
        for slot in range(3):
            if slot==2 and self.mapper[0]&8:
                start = 16384 if self.mapper[0]&4 else 0
                self.memory[32768:49152] = self.sram[start:start+16384]
            else:
                bank = self.mapper[slot+1]%pages
                self.memory[slot*16384:(slot+1)*16384] = self.rom[bank*16384:(bank+1)*16384]
        self.memory[:1024] = self.rom[:1024]

    def write(self, address, value):
        address &= 65535; value &= 255
        if address>=49152:
            ram = 49152+(address&8191)
            self.memory[ram] = self.memory[ram+8192] = value
            # Only exact FFFC-FFFF addresses clock the mapper; RAM aliases do not.
            if address>=65532:
                self.mapper[address-65532] = value; self.remap()
        elif address>=32768 and self.mapper[0]&8:
            self.sram[(16384 if self.mapper[0]&4 else 0)+(address&16383)] = value
            self.memory[address] = value

    def release_keys(self): self.keys.clear()
    def set_keys(self, keys): self.keys=set(keys)

    def in_port(self, port):
        p=port&255
        if p == 0:
            # Export region, NTSC clock; START is active-low, not an NMI.
            return 0x40 | (0 if self.keys & {'return','kp_enter','start'} else 0x80)
        if 1 <= p <= 5:
            # Disconnected link port approximation, not a link-cable emulator.
            if p == 1:
                value = self.gg_ports[1]
                ext = (value&128) | ((value|self.gg_ports[2])&127)
                # Serial TX/RX remain idle high; no transfer or link interrupt.
                if self.gg_ports[5]&16: ext |= 16
                if self.gg_ports[5]&32: ext |= 32
                return ext
            return self.gg_ports[p]
        if p&192 == 64: return self.h_latch if p&1 else self.vdp.vcounter()
        if p&192 == 128: return self.vdp.read_status() if p&1 else self.vdp.read_data()
        if p in (0xC0,0xC1,0xDC,0xDD):
            ext = self.in_port(1)
            if not p&1:
                value=63 | ((ext&3)<<6)
                for bit,key in enumerate(('up','down','left','right','z','x')):
                    if key in self.keys: value &= ~(1<<bit)
                return value
            # EXT pins reflect here even when used as disconnected outputs.
            return 0x70 | ((ext>>2)&15) | ((ext&64)<<1)
        return 255

    def out_port(self, port, value):
        p=port&255; value&=255
        if p == 6:
            self.psg.stereo = value
        elif 1 <= p <= 5:
            if p == 1: self.gg_ports[1] = value
            elif p == 2: self.gg_ports[2] = value
            elif p == 3: self.gg_ports[3] = value
            elif p == 5: self.gg_ports[5] = value&0xF8
            # RX is read-only. Serial control is stored without transfer/NMI.
        elif p == 0:
            pass
        elif p<64:
            if p == 0x3E: self.mem_control=value
            # Native-mode I/O 3F and low-port mirrors are not modeled.
            # The SMS I/O-disable bit has no effect in native GG mode.
        elif p<128:
            self.psg.write(value)
        elif p<192:
            if p&1: self.vdp.control(value)
            else: self.vdp.write_data(value)

    def run(self, cycles, deadline=None):
        end=self.cpu.tstates+max(0,cycles); start=self.cpu.tstates; count=0
        while self.cpu.tstates<end:
            used=self.cpu.interrupt() if self.vdp.irq else 0
            if not used: used=self.cpu.step()
            self.vdp.tick(used); self.psg.advance(used); count+=1
            if deadline is not None and count%128==0 and time.perf_counter()>=deadline: break
        return self.cpu.tstates-start


def demo_rom():
    """Assemble an original, hardware-addressed Z80 game in memory.
    Every moving pixel and score update is driven by this ROM via CPU/VDP I/O.
    This generator does not know the running machine and never writes a file.
    """
    rom=bytearray(32768); code=bytearray(); labels={}; fixups=[]
    def emit(*values): code.extend(v&255 for v in values)
    def word(value): emit(value,value>>8)
    def label(name): labels[name]=len(code)
    def jump(op,name): emit(op); fixups.append((len(code),name)); word(0)
    def load(address): emit(0x3A); word(address)
    def save(address): emit(0x32); word(address)
    def immediate(value): emit(0x3E,value)
    def out(port): emit(0xD3,port)
    def vaddr(address): immediate(address&255); out(0xBF); immediate(0x40|(address>>8)); out(0xBF)
    def register(n,value): immediate(value); out(0xBF); immediate(0x80|n); out(0xBF)
    def data_copy(name,data):
        emit(0x21); fixups.append((len(code),name)); word(0)
        emit(0x11); word(len(data)); jump(0xCD,'copy')
    jump(0xC3,'start')
    while len(code)<0x38: emit(0)
    emit(0xF5,0xDB,0xBF,0xF1,0xFB,0xC9)  # IRQ acknowledge and return.
    while len(code)<0x66: emit(0)
    emit(0xED,0x45)  # No START NMI; link interrupts are not modeled.
    label('start'); emit(0xF3,0x31,0xE0,0xDF)
    for n,val in enumerate((6,0x80,0x0E,0xFF,7,0x7E,0,0,0,0,255)): register(n,val)
    # Silence channels even on real hardware.
    for n in (0x9F,0xBF,0xDF,0xFF): immediate(n); out(0x7F)
    fonts={
        'A':['01110','10001','10001','11111','10001','10001','10001'],
        'B':['11110','10001','10001','11110','10001','10001','11110'],
        'C':['01111','10000','10000','10000','10000','10000','01111'],
        'E':['11111','10000','10000','11110','10000','10000','11111'],
        'G':['01111','10000','10000','10111','10001','10001','01111'],
        'H':['10001','10001','10001','11111','10001','10001','10001'],
        'I':['11111','00100','00100','00100','00100','00100','11111'],
        'M':['10001','11011','10101','10101','10001','10001','10001'],
        'N':['10001','11001','11001','10101','10011','10011','10001'],
        'O':['01110','10001','10001','10001','10001','10001','01110'],
        'P':['11110','10001','10001','11110','10000','10000','10000'],
        'R':['11110','10001','10001','11110','10100','10010','10001'],
        'S':['01111','10000','10000','01110','00001','00001','11110'],
        'T':['11111','00100','00100','00100','00100','00100','00100'],
        'U':['10001','10001','10001','10001','10001','10001','01110'],
        'V':['10001','10001','10001','10001','10001','01010','00100'],
        'W':['10001','10001','10001','10101','10101','11011','10001'],
        'X':['10001','10001','01010','00100','01010','10001','10001'],
        'Z':['11111','00001','00010','00100','01000','10000','11111'],
        '0':['01110','10001','10011','10101','11001','10001','01110'],
        '1':['00100','01100','00100','00100','00100','00100','01110'],
        '2':['01110','10001','00001','00010','00100','01000','11111'],
        '3':['11110','00001','00001','01110','00001','00001','11110'],
        '4':['00010','00110','01010','10010','11111','00010','00010'],
        '5':['11111','10000','10000','11110','00001','00001','11110'],
        '6':['01110','10000','10000','11110','10001','10001','01110'],
        '7':['11111','00001','00010','00100','01000','01000','01000'],
        '8':['01110','10001','10001','01110','10001','10001','01110'],
        '9':['01110','10001','10001','01111','00001','00001','01110'],
    }
    tiles=bytearray(96*32)
    def tile(n,rows):
        for y,row in enumerate(rows):
            for x,value in enumerate(row):
                for bit in range(4):
                    if int(value)&(1<<bit): tiles[n*32+y*4+bit]|=1<<(7-x)
    tile(1,['00011000','00133100','01333310','13333331','00133100','00133100','01300310','01000010'])
    tile(2,['00022000','00033000','22333322','03333330','00333300','02300320','02000020','00000000'])
    tile(3,['11111111']+['10000001']*6+['11111111'])
    for char,rows in fonts.items(): tile(ord(char),['0'+''.join('3' if c=='1' else '0' for c in row)+'00' for row in rows]+['00000000'])
    names=bytearray(32*28*2)
    def text(x,y,line):
        for n,ch in enumerate(line): names[(y*32+x+n)*2]=ord(ch)
    text(11,4,'STAR CHASE'); text(12,6,'SCORE 00')
    text(8,19,'ARROWS  Z BOOST'); text(8,20,'X TARGET ENTER')
    for x in range(7,25): names[(8*32+x)*2]=names[(18*32+x)*2]=3
    for y in range(9,18): names[(y*32+7)*2]=names[(y*32+24)*2]=3
    palette=b''.join(c.to_bytes(2,'little') for c in ([0x210,0x742,0x1DF,0xFED]+[0]*12+[0x210,0xFD3,0x1DF,0xFFF]+[0]*12))
    vaddr(0); data_copy('tiles',tiles)
    vaddr(0x3800); data_copy('names',names)
    immediate(0); out(0xBF); immediate(0xC0); out(0xBF); data_copy('palette',palette)
    for address,value in ((0xC000,80),(0xC001,104),(0xC002,160),(0xC003,104),(0xC004,0),(0xC006,0),(0xC008,255),(0xC009,128)):
        immediate(value); save(address)
    register(1,0xC0); immediate(1); save(0xC007)
    jump(0xCD,'sprites')
    label('wait'); emit(0xDB,0xBF,0xE6,128); jump(0xCA,'wait')
    # Poll the actual native-mode START port, edge-triggered in ROM software.
    load(0xC009); emit(0x57,0xDB,0x00); save(0xC009)
    emit(0xAA,0xA2,0xE6,128); jump(0xCA,'start_done')
    load(0xC006); emit(0xEE,1); save(0xC006)
    label('start_done')
    immediate(0x9F); out(0x7F); immediate(0xFF); out(0x7F)
    load(0xC006); emit(0xB7); jump(0xC2,'wait')
    emit(0xDB,0xDC); save(0xC005)
    # Tone follows button 1. Its stereo side follows the player position.
    emit(0xE6,16); jump(0xC2,'tone_done')
    for value in (0x8E,0x0F,0x96): immediate(value); out(0x7F)
    label('tone_done'); load(0xC000); emit(0xFE,128); jump(0xD2,'sound_right')
    immediate(0x90); jump(0xC3,'sound_side')
    label('sound_right'); immediate(0x09)
    label('sound_side'); out(6)
    load(0xC005); emit(0xE6,32); jump(0xC2,'noise_done')
    immediate(0xE4); out(0x7F); immediate(0xF8); out(0x7F)
    label('noise_done'); load(0xC005)
    # B contains speed (button 1 boosts). C holds active-low controls.
    emit(0x4F,0x06,1,0xCB,0x61); jump(0xC2,'speed_ready'); emit(0x06,2)
    label('speed_ready')
    for name,bit,address,lower,upper,decrement in (
        ('up',0,0xC001,73,136,True),('down',1,0xC001,73,136,False),
        ('left',2,0xC000,65,184,True),('right',3,0xC000,65,184,False)):
        emit(0xCB,0x41+bit*8); jump(0xC2,name+'_done')
        load(address); emit(0x90 if decrement else 0x80)
        emit(0xFE,lower if decrement else upper)
        jump(0xD2 if decrement else 0xDA,name+'_ok')
        immediate(lower if decrement else upper)
        label(name+'_ok'); save(address); label(name+'_done')
    # X advances the target on the first down edge only.
    load(0xC008); emit(0xE6,32,0x57); load(0xC005); save(0xC008)
    emit(0xE6,32,0xAA,0xA2); jump(0xC2,'target')
    load(0xC002); emit(0x57); load(0xC000); emit(0x92,0xC6,6,0xFE,13); jump(0xD2,'draw')
    load(0xC003); emit(0x57); load(0xC001); emit(0x92,0xC6,6,0xFE,13); jump(0xD2,'draw')
    load(0xC004); emit(0xC6,1,0x27); save(0xC004)  # BCD score, wraps at 100.
    vaddr(0x3800+(6*32+18)*2)
    load(0xC004); emit(0x0F,0x0F,0x0F,0x0F,0xE6,15,0xC6,48); out(0xBE)
    immediate(0); out(0xBE)
    load(0xC004); emit(0xE6,15,0xC6,48); out(0xBE); immediate(0); out(0xBE)
    label('target')
    for name,address,increment,upper,span in (('tx',0xC002,43,184,119),('ty',0xC003,23,136,63)):
        load(address); emit(0xC6,increment); jump(0xDA,name+'_wrap')
        emit(0xFE,upper); jump(0xDA,name+'_ready')
        label(name+'_wrap'); emit(0xD6,span)
        label(name+'_ready'); save(address)
    label('draw'); jump(0xCD,'sprites'); jump(0xC3,'wait')
    label('sprites')
    vaddr(0x3F00); load(0xC001); emit(0x3D); out(0xBE); load(0xC003); emit(0x3D); out(0xBE); immediate(208); out(0xBE)
    vaddr(0x3F80); load(0xC000); out(0xBE); immediate(1); out(0xBE); load(0xC002); out(0xBE); immediate(2); out(0xBE); emit(0xC9)
    label('copy'); emit(0x7E,0xD3,0xBE,0x23,0x1B,0x7A,0xB3); jump(0xC2,'copy'); emit(0xC9)
    for name,data in (('tiles',tiles),('names',names),('palette',palette)):
        label(name); code.extend(data)
    for offset,name in fixups: code[offset:offset+2]=labels[name].to_bytes(2,'little')
    assert len(code)<0x7FF0
    rom[:len(code)]=code
    # An ordinary native Game Gear export header, not copied from any existing game.
    rom[0x7FF0:0x7FF8]=b'TMR SEGA'; rom[0x7FFF]=0x6C
    checksum=sum(rom[:0x7FF0])&65535; rom[0x7FFA:0x7FFC]=checksum.to_bytes(2,'little')
    return bytes(rom)


class AudioOutput:
    """Optional playback via installed pygame or ffplay. Never writes files.

    ffplay receives raw PCM on stdin through one bounded queue/worker thread.
    Pause/mute/close kill queued playback so stale sound cannot leak on resume.
    No backend is installed or downloaded automatically.
    """
    def __init__(self):
        self.backend = None
        self.reason = "not started"
        self.pygame = None
        self.channel = None
        self.process = None
        self.thread = None
        self.stop_event = None
        self.queue = None
        self.owned_mixer = False
        self.closed = False
        self._attempted_pygame = False

    @property
    def available(self):
        if self.backend == "ffplay" and self.process and self.process.poll() is not None:
            self.backend = None
            self.reason = "audio playback unavailable"
        return self.backend is not None

    def start(self):
        if self.closed:
            return False
        if self.available:
            return True
        if self.process:
            self.stop()
        if not self._attempted_pygame:
            self._attempted_pygame = True
            try:
                import os
                os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
                import pygame
                self.pygame = pygame
                if not pygame.mixer.get_init():
                    self.owned_mixer = True
                    pygame.mixer.init(frequency=PSG.SAMPLE_RATE, size=-16, channels=2, buffer=512, allowedchanges=0)
                if pygame.mixer.get_init() != (PSG.SAMPLE_RATE, -16, 2):
                    raise RuntimeError("mixer format differs from 44100 Hz stereo")
                self.channel = pygame.mixer.Channel(0)
                self.backend, self.reason = "pygame", "ready"
                return True
            except Exception:
                if self.owned_mixer and self.pygame:
                    try:
                        self.pygame.mixer.quit()
                    except Exception:
                        pass
                self.pygame = self.channel = None
                self.owned_mixer = False
        import shutil
        player = shutil.which("ffplay")
        if not player:
            self.reason = "install pygame or provide ffplay for playback"
            return False
        import queue
        import subprocess
        import threading
        try:
            process = subprocess.Popen([player, "-nodisp", "-autoexit", "-loglevel", "quiet",
                                        "-f", "s16le", "-ar", str(PSG.SAMPLE_RATE), "-ch_layout", "stereo",
                                        "-probesize", "32", "-analyzeduration", "0", "-i", "pipe:0"],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, bufsize=0)
        except (OSError, ValueError):
            self.reason = "audio player could not start"
            return False
        pending = queue.Queue(maxsize=4)
        stop = threading.Event()
        self.process, self.queue, self.stop_event = process, pending, stop
        self.backend, self.reason = "ffplay", "ready"
        def worker():
            try:
                while not stop.is_set():
                    try:
                        data = pending.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    view = memoryview(data)
                    while view and not stop.is_set():
                        count = process.stdin.write(view)
                        if not count:
                            raise BrokenPipeError("audio pipe closed")
                        view = view[count:]
            except (BrokenPipeError, OSError, ValueError):
                if not stop.is_set():
                    self.reason = "audio playback unavailable"
            finally:
                if process.poll() is None:
                    process.terminate()
        self.thread = threading.Thread(target=worker, name="BlueGear-audio", daemon=True)
        self.thread.start()
        return True

    def submit(self, pcm):
        if not pcm or not self.available:
            return
        if self.backend == "pygame":
            try:
                sound = self.pygame.mixer.Sound(buffer=pcm)
                if not self.channel.get_busy():
                    self.channel.play(sound)
                elif self.channel.get_queue() is None:
                    self.channel.queue(sound)
            except Exception:
                self.stop()
                if self.owned_mixer:
                    try:
                        self.pygame.mixer.quit()
                    except Exception:
                        pass
                self.pygame = self.channel = self.backend = None
                self.reason = "audio playback unavailable"
        else:
            import queue
            try:
                self.queue.put_nowait(pcm)
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.queue.put_nowait(pcm)
                except queue.Full:
                    pass

    def stop(self):
        if self.channel is not None:
            try:
                self.channel.stop()
            except Exception:
                pass
        if self.stop_event:
            self.stop_event.set()
        if self.process:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                self.process.wait(timeout=1)
            except Exception:
                try:
                    self.process.kill()
                    self.process.wait(timeout=1)
                except Exception:
                    pass
            if self.thread:
                self.thread.join(timeout=1)
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
        self.process = self.thread = self.queue = self.stop_event = None
        if self.pygame is None:
            self.backend = None

    def close(self):
        self.stop()
        if self.owned_mixer and self.pygame:
            try:
                self.pygame.mixer.quit()
            except Exception:
                pass
        self.backend = None
        self.closed = True


HELP = '''BLUE GEAR / EXPERIMENTAL GAME GEAR EMULATOR

Python 3.10+ with Tkinter. One standalone file, no BIOS or assets required.

STAR CHASE is an original Z80 ROM generated in memory. Collect yellow stars with the cyan ship. Arrows move, Z boosts and plays a tone, X advances the target and plays noise. Enter is Game Gear START; the demo polls it to toggle its own pause. Enable Sound to hear the PSG. Tone/noise pan left or right with the ship.

CONTROLS
Arrows: D-pad    Z / X: buttons 1 / 2
Enter: START (active-low port 00, not an NMI)
F5: emulator Play/Pause    F6: Sound on/off
F8: cold reset    F1: Help    Escape: Pause
Focus loss releases keys. Help/file dialogs pause emulation and playback.

LOAD
Load a legally obtained raw .gg/.bin/.rom file: 8 KiB to 4 MiB, in 8 KiB increments. A 512-byte copier header is recognized. ROM starts at 0000 without BIOS. Standard Sega mapper only; unsupported mapper types are not detected. Cancel or invalid files preserve the current machine.

IMPLEMENTED
Instruction-timed Z80, 8 KiB mirrored RAM, Sega ROM banking and 32 KiB volatile cartridge RAM; 256x192 Mode 4 internal raster with actual 160x144 LCD crop; 64-byte CRAM with even-byte latch/odd-byte 12-bit palette commit; tiles, scrolling, priority, sprites, line/frame IRQ; native START input, D-pad and two buttons; three-tone/noise PSG with Game Gear stereo routing.

SOUND
Optional, initially off. Playback uses an already-installed pygame or ffplay and a working audio device. No install/download is attempted. PCM and bounded queues stay in RAM. If playback is unavailable, the app remains usable silently. Timing, noise phase and analog filtering are approximate. High-frequency aliasing and sampled-voice/DC behavior are not reproduced faithfully.

LIMITS
Experimental compatibility. Native Game Gear mode only, fixed 192-line raster and NTSC timing. No SMS-converter mode, extended-height/legacy VDP modes, BIOS, Codemasters/Korean mappers, EEPROM, FM, serial/parallel link transfers or link interrupts. Disconnected link registers are approximated. Memory-control disabling and mapper control bits beyond SRAM enable/bank are ignored. CPU/VDP timing is instruction/scanline-based, not bus/pixel-exact. Some raster effects, counters and cartridge behavior can be wrong. General commercial-game compatibility is unverified. Speed depends on your host; the display reports actual emulated progress.

No settings, ROMs, saves or audio files are written. RAM and cartridge SRAM are volatile; reset or close loses state. No copyrighted games or BIOS are bundled.''' 


class App:
    def __init__(self, root):
        import tkinter as tk
        self.tk=tk; self.root=root; self.machine=GameGear()
        self.running=True; self.closed=False; self.dialog_open=False; self.help_window=None
        self.pressed=set(); self.release_jobs={}; self.photo=None
        self.audio=AudioOutput(); self.sound_enabled=False
        self.last_time=time.perf_counter(); self.last_render=0; self.credit=0
        self.speed_time=self.last_time; self.speed_cycles=0; self.last_frame=-1
        root.title('Blue Gear | Game Gear'); root.resizable(False,False)
        root.configure(bg='#030a14')
        root.update_idletasks()
        ww,hh=600,400
        sx=max(0,(root.winfo_screenwidth()-ww)//2); sy=max(0,(root.winfo_screenheight()-hh)//2)
        root.geometry(f'{ww}x{hh}+{sx}+{sy}')
        self.status=tk.StringVar(value='Original Z80 demo  |  Arrows move · Z boost · Enter START')
        self.audio_text=tk.StringVar(value='Sound off')
        self.speed=tk.StringVar(value='59.9 Hz target'); self.source=tk.StringVar(value='ORIGINAL DEMO')
        # In-window menustrip (macOS Tk hides top-level root Menu commands under "Python").
        strip=tk.Frame(root,bg='#0c1d30',highlightthickness=0)
        strip.place(x=0,y=0,width=600,height=26)
        def menu_btn(text,fn):
            return tk.Button(strip,text=text,command=fn,bg='#0c1d30',fg='#e1f1ff',
                activebackground='#195688',activeforeground='white',relief='flat',bd=0,
                font=('Helvetica',10),padx=10,pady=2,takefocus=False,cursor='hand2')
        for label,fn in (
            ('Load ROM',self.open_rom),('Exit',self.close),('Help',self.help),
            ('About',self.about),('Play Game',self.play_game)):
            menu_btn(label,fn).pack(side='left',padx=1,pady=1)
        # Also wire a normal cascade menubar so the macOS app menu is not empty.
        menubar=tk.Menu(root); self.menubar=menubar
        file_m=tk.Menu(menubar,tearoff=0)
        file_m.add_command(label='Load ROM',command=self.open_rom)
        file_m.add_command(label='Play Game',command=self.play_game)
        file_m.add_separator(); file_m.add_command(label='Exit',command=self.close)
        menubar.add_cascade(label='File',menu=file_m)
        help_m=tk.Menu(menubar,tearoff=0)
        help_m.add_command(label='Help',command=self.help)
        help_m.add_command(label='About Blue Gear',command=self.about)
        menubar.add_cascade(label='Help',menu=help_m)
        root.config(menu=menubar)
        try: root.createcommand('tkAboutDialog',self.about)
        except tk.TclError: pass
        tk.Label(root,text='BLUE GEAR',bg='#030a14',fg='#51b4ff',font=('Helvetica',16,'bold')).place(x=18,y=30)
        tk.Label(root,text='GAME GEAR  ·  160 × 144',bg='#030a14',fg='#7d9dbd',font=('Helvetica',9,'bold')).place(x=148,y=38)
        tk.Label(root,textvariable=self.source,bg='#030a14',fg='#7bc4ff',font=('Helvetica',8,'bold')).place(x=470,y=38)
        # Game Gear LCD is 160×144; zoom×2 → 320×288, centered in the fixed window.
        self.canvas=tk.Canvas(root,width=320,height=288,bg='#000000',highlightthickness=2,highlightbackground='#1c5382')
        self.canvas.place(x=(600-320)//2,y=58); self.image_id=self.canvas.create_image(0,0,anchor='nw')
        self.canvas.bind('<Button-1>',lambda e:self.canvas.focus_set())
        tk.Label(root,text='ARROWS / Z X  ·  F5 Play/Pause  ·  F6 Sound  ·  F8 Reset',bg='#030a14',fg='#a4d4f8',font=('Helvetica',8)).place(x=18,y=350)
        tk.Label(root,textvariable=self.speed,bg='#030a14',fg='#7e9dbb',font=('Helvetica',8)).place(x=420,y=350)
        tk.Label(root,textvariable=self.audio_text,bg='#030a14',fg='#527594',font=('Helvetica',8)).place(x=18,y=366)
        tk.Label(root,textvariable=self.status,bg='#0c1d30',fg='#a8c8e4',font=('Helvetica',9),anchor='w',padx=12).place(x=0,y=376,width=600,height=24)
        root.bind('<KeyPress>',self.key_down); root.bind('<KeyRelease>',self.key_up); root.bind('<FocusOut>',self.focus_lost)
        root.protocol('WM_DELETE_WINDOW',self.close); self.canvas.focus_set(); self.render(); self.after_id=root.after(1,self.tick)

    def key_down(self,event):
        key=event.keysym.lower()
        pending=self.release_jobs.pop(key,None)
        if pending is not None: self.root.after_cancel(pending)
        if self.dialog_open or key in self.pressed: return 'break'
        self.pressed.add(key)
        if key=='f5': self.toggle()
        elif key=='f6': self.toggle_sound()
        elif key=='f8': self.reset()
        elif key=='f1': self.help()
        elif key=='escape': self.set_running(False)
        else: self.machine.set_keys(self.pressed if self.running else set())
        return 'break'

    def key_up(self,event):
        key=event.keysym.lower()
        if key in self.release_jobs: self.root.after_cancel(self.release_jobs[key])
        # X11 sends a synthetic release/press pair on repeat. Delay release briefly.
        def release():
            self.release_jobs.pop(key,None); self.pressed.discard(key); self.machine.set_keys(self.pressed if self.running else set())
        self.release_jobs[key]=self.root.after(12,release)
        return 'break'

    def focus_lost(self,event=None):
        for job in self.release_jobs.values(): self.root.after_cancel(job)
        self.release_jobs.clear(); self.pressed.clear(); self.machine.release_keys()

    def sync_audio(self):
        self.machine.psg.drain(); self.audio.stop()
        if self.sound_enabled and self.running:
            if self.audio.start(): self.audio_text.set('Stereo PSG via '+str(self.audio.backend))
            else: self.audio_text.set('Audio unavailable')
        else: self.audio_text.set('Sound paused' if self.sound_enabled else 'Sound off')

    def toggle_sound(self):
        self.sound_enabled=not self.sound_enabled
        self.sync_audio(); self.canvas.focus_set()

    def set_running(self,value):
        self.running=value; self.credit=0; self.last_time=time.perf_counter()
        self.speed_time=self.last_time; self.speed_cycles=0
        self.speed.set('Measuring...' if value else 'Paused')
        self.machine.set_keys(self.pressed if value else set())
        self.sync_audio(); self.update_status()

    def toggle(self): self.set_running(not self.running)

    def play_game(self):
        """Menu: start if paused, pause if already playing."""
        self.set_running(not self.running)
        self.canvas.focus_set()

    def update_status(self):
        if self.machine.vdp.reg[1]&64 and not self.machine.vdp.supported:
            self.status.set('Unsupported VDP mode  |  Native 192-line Mode 4 only')
        else: self.status.set(('Playing' if self.running else 'Paused')+'  |  '+self.machine.title[:65])

    def reset(self):
        self.machine.reset(); self.credit=0; self.focus_lost(); self.sync_audio(); self.render(); self.update_status()

    def start_demo(self):
        self.machine=GameGear(); self.focus_lost(); self.render(); self.source.set('ORIGINAL DEMO'); self.set_running(True); self.canvas.focus_set()

    def open_rom(self):
        if self.dialog_open: return
        from tkinter import filedialog,messagebox
        resume=self.running; self.set_running(False); self.dialog_open=True; self.focus_lost()
        try:
            path=filedialog.askopenfilename(parent=self.root,title='Load your Game Gear ROM',filetypes=[('Game Gear ROM','*.gg *.bin *.rom'),('All files','*')])
            if not path: self.set_running(resume); return
            with open(path,'rb') as f: data=f.read(4*1024*1024+513)
            self.machine.load_rom(data,os.path.basename(path)); self.source.set('SEGA MAPPER'); self.render(); self.set_running(True)
        except (ValueError,OSError) as error:
            messagebox.showerror('ROM not loaded',str(error),parent=self.root); self.set_running(resume)
        finally:
            self.dialog_open=False; self.focus_lost(); self.canvas.focus_set()

    def help(self):
        if self.help_window is not None and self.help_window.winfo_exists(): self.help_window.lift(); return
        tk=self.tk; resume=self.running; self.set_running(False); self.dialog_open=True; self.focus_lost()
        window=tk.Toplevel(self.root); self.help_window=window
        window.title('Blue Gear - Help'); window.geometry('540x360'); window.resizable(False,False); window.configure(bg='#0c1d30'); window.transient(self.root)
        body=tk.Frame(window,bg='#0c1d30'); body.place(x=14,y=14,width=512,height=294)
        scroll=tk.Scrollbar(body); scroll.pack(side='right',fill='y')
        text=tk.Text(body,wrap='word',bg='#0c1d30',fg='#d7edff',relief='flat',font=('Helvetica',10),yscrollcommand=scroll.set,padx=4,pady=4)
        text.pack(side='left',fill='both',expand=True); scroll.configure(command=text.yview); text.insert('1.0',HELP); text.configure(state='disabled')
        def finish(event=None):
            if self.help_window is not window: return
            self.help_window=None; window.grab_release(); window.destroy(); self.dialog_open=False
            self.focus_lost(); self.set_running(resume); self.canvas.focus_set(); return 'break'
        tk.Button(window,text='Close',command=finish,bg='#113356',fg='#e1f1ff',activebackground='#195688',relief='flat').place(x=216,y=320,width=108,height=28)
        window.protocol('WM_DELETE_WINDOW',finish); window.bind('<Escape>',finish); window.grab_set(); window.focus_set()

    def about(self):
        from tkinter import messagebox
        messagebox.showinfo(
            'About Blue Gear',
            'Blue Gear 0.1.1 (infdev)\nSega Game Gear native-mode emulator\n\n'
            '160×144 LCD · Z80 · VDP Mode 4 · SN76489 PSG\n'
            'Load a .gg ROM from the menu, or play the built-in demo.\n\n'
            'Controls: Arrows · Z/X · Enter START\n'
            'F5 Play/Pause · F6 Sound · F8 Reset · F1 Help',
            parent=self.root)
        self.canvas.focus_set()

    def render(self):
        raw=self.tk.PhotoImage(data=b'P6\n160 144\n255\n'+self.machine.vdp.front,format='PPM')
        self.photo=raw.zoom(2); self.canvas.itemconfigure(self.image_id,image=self.photo)
        self.last_frame=self.machine.vdp.frames

    def tick(self):
        if self.closed:return
        now=time.perf_counter(); elapsed=min(.1,now-self.last_time); self.last_time=now
        if self.running:
            self.credit=min(self.credit+elapsed*self.machine.CLOCK,self.machine.FRAME*2)
            if self.credit>0:
                try:
                    actual=self.machine.run(int(self.credit),time.perf_counter()+.010)
                    self.credit-=actual; self.speed_cycles+=actual
                except Exception as error:
                    self.set_running(False); self.status.set('Stopped: '+str(error)[:80])
        pcm=self.machine.psg.drain()
        if self.sound_enabled and self.running:
            self.audio.submit(pcm)
            if not self.audio.available: self.audio_text.set('Audio unavailable')
        if now-self.last_render>=1/30 and self.last_frame!=self.machine.vdp.frames:
            self.render(); self.last_render=now
        if now-self.speed_time>=1:
            percent=round(100*self.speed_cycles/((now-self.speed_time)*self.machine.CLOCK))
            self.speed.set(f'{percent}% speed' if self.running else 'Paused'); self.speed_cycles=0; self.speed_time=now; self.update_status()
        if self.running and self.credit>500: self.after_id=self.root.after_idle(self.tick)
        else: self.after_id=self.root.after(1,self.tick)

    def close(self):
        self.closed=True; self.focus_lost(); self.audio.close()
        if self.after_id: self.root.after_cancel(self.after_id)
        self.after_id=None
        self.root.destroy()


def self_test():
    """Deterministic offline hardware checks; no display or external ROM needed."""
    import unittest
    def vaddr(v,a,code=1): v.control(a&255);v.control((code<<6)|(a>>8))
    def put(v,a,data):
     vaddr(v,a)
     for b in data:v.write_data(b)
    def configure(v):
     v.reg[0]=6;v.reg[1]=64;v.reg[2]=14;v.reg[5]=126
     vaddr(v,0,3)
     for n in range(32):v.write_data(n);v.write_data(n>>4)
     put(v,0x3F00,[208])
    def solid(v,tile,color):put(v,tile*32,[(255 if color&(1<<b) else 0) for y in range(8) for b in range(4)])
    def pixel(v,x,y=0):return bytes(v.rgb[(y*256+x)*3:(y*256+x+1)*3])
    def sprite(v,index,x,y,tile):put(v,0x3F00+index,[y-1&255]);put(v,0x3F80+index*2,[x,tile]);put(v,0x3F00+index+1,[208])
    class MemoryTests(unittest.TestCase):
     def setUp(self):self.m=GameGear(b''.join(bytes([n])*16384 for n in range(8)))
     def test_default_banks(self):self.assertEqual([self.m.memory[a] for a in (0,0x4000,0x8000)],[0,1,2])
     def test_mapper_slots_fixed_first_k(self):
      self.m.write(0xFFFD,4);self.m.write(0xFFFE,5);self.m.write(0xFFFF,6)
      self.assertEqual([self.m.memory[a] for a in (0,1023,1024,0x4000,0x8000)],[0,0,4,5,6])
     def test_bank_wrap(self):self.m.write(0xFFFF,255);self.assertEqual(self.m.memory[0x8000],7)
     def test_register_readback(self):self.m.write(0xFFFE,7);self.assertEqual(self.m.memory[0xDFFE],7)
     def test_alias_not_mapper(self):self.m.write(0xDFFE,7);self.assertEqual(self.m.mapper[2],1)
     def test_ram_mirror(self):
      for a in (0xC000,0xDFFF,0xE456,0xFFFA):self.m.write(a,173);self.assertEqual(self.m.memory[a^0x2000],173)
     def test_rom_immutable(self):self.m.write(100,174);self.assertEqual(self.m.memory[100],0)
     def test_sram_banks(self):
      self.m.write(0xFFFC,8);self.m.write(0x8123,33);self.m.write(0xFFFC,12);self.m.write(0x8123,44)
      self.m.write(0xFFFC,8);self.assertEqual(self.m.memory[0x8123],33)
      self.m.write(0xFFFC,12);self.assertEqual(self.m.memory[0x8123],44)
      self.m.write(0xFFFC,0);self.assertEqual(self.m.memory[0x8123],2)
     def test_eight_k_rom(self):
      m=GameGear(bytes([17])*8192);self.assertEqual(len(m.memory),65536);self.assertEqual(m.memory[0x8000],17)
     def test_copier_header(self):self.assertEqual(GameGear.normalize_rom(bytes(512)+demo_rom()),demo_rom())
     def test_bad_load_transaction(self):
      before=bytes(self.m.memory)
      for data in (b'',bytes(8191),bytes(9000),bytes(4*1024*1024+8192)):
       with self.assertRaises(ValueError):self.m.load_rom(data)
       self.assertEqual(bytes(self.m.memory),before)
     def test_24k_padding(self):
      m=GameGear(bytes(24576));self.assertEqual(len(m.memory),65536);self.assertEqual(m.memory[0x6000],255)
    class VDPTests(unittest.TestCase):
     def test_buffered_read_prefetch(self):
      v=VDP();put(v,0x123,[6,7,8]);vaddr(v,0x123,0)
      self.assertEqual([v.read_data(),v.read_data(),v.read_data()],[6,7,8])
     def test_address_wrap(self):v=VDP();put(v,16383,[44,55]);self.assertEqual((v.vram[-1],v.vram[0],v.address),(44,55,1))
     def test_palette_mask_wrap(self):
      v=VDP();vaddr(v,62,3)
      for b in (255,255,15,0):v.write_data(b)
      self.assertEqual(bytes(v.cram[62:]),bytes([255,15]));self.assertEqual(v.palette[31],b'\xff'*3)
      self.assertEqual(v.palette[0],b'\xff\x00\x00')
     def test_palette_even_latch_only(self):
      v=VDP();vaddr(v,2,3);v.write_data(0xAD)
      self.assertEqual(bytes(v.cram),bytes(64));self.assertEqual(v.cram_latch,0xAD)
     def test_palette_nonadjacent_odd_commit(self):
      v=VDP();vaddr(v,0,3);v.write_data(0xAD);vaddr(v,33,3);v.write_data(7)
      self.assertEqual(v.palette[16],bytes([221,170,119]));self.assertEqual(v.palette[0],bytes(3))
     def test_exact_lcd_crop(self):
      v=VDP()
      for y in range(192):
       for x in range(256):v.rgb[(y*256+x)*3:(y*256+x+1)*3]=bytes([x,y,0])
      p=v.lcd_pixels();self.assertEqual(len(p),160*144*3)
      self.assertEqual(p[:3],bytes([48,24,0]));self.assertEqual(p[-3:],bytes([207,167,0]))
     def test_registers(self):
      v=VDP();v.control(84);v.control(0x88);v.control(93);v.control(0x8B)
      self.assertEqual((v.reg[8],v.reg[11]),(84,0))
     def test_data_cancels_latch(self):v=VDP();v.control(9);v.write_data(22);self.assertIsNone(v.latch)
     def test_status_clears_irqs_latch(self):
      v=VDP();v.reg[0]=16;v.reg[1]=32;v.frame_pending=v.line_pending=True;v.status=224;v.control(3)
      self.assertTrue(v.irq);self.assertEqual(v.read_status(),224);self.assertFalse(v.irq);self.assertIsNone(v.latch)
     def test_frame_counters(self):
      v=VDP();v.tick(228*262);self.assertEqual((v.frames,v.line,v.cycles),(1,0,0));self.assertTrue(v.status&128)
     def test_counter_jump(self):
      v=VDP();v.line=218;self.assertEqual(v.vcounter(),218);v.line=219;self.assertEqual(v.vcounter(),213)
     def test_line_irq(self):
      v=VDP();v.reg[0]=16;v.reg[10]=2;v.line_counter=2
      v.tick(228*2);self.assertFalse(v.irq);v.tick(228);self.assertTrue(v.irq)
     def test_frame_irq_enable_after_pending(self):
      v=VDP();v.tick(228*193);self.assertFalse(v.irq);v.reg[1]=32;self.assertTrue(v.irq)
     def test_pattern_bitplanes(self):
      v=VDP();put(v,0,[0x80,0x40,0x20,0x10]);self.assertEqual(v.pattern(0,0),(1,2,4,8,0,0,0,0))
     def test_pattern_cache_invalidates(self):
      v=VDP();self.assertEqual(v.pattern(1,0),(0,)*8);put(v,32,[255]);self.assertEqual(v.pattern(1,0),(1,)*8)
     def test_background_palette(self):
      v=VDP();configure(v);solid(v,1,2);put(v,0x3800,[1,8]);v.render_line(0);self.assertEqual(pixel(v,0),v.palette[18])
     def test_horizontal_flip(self):
      v=VDP();configure(v);put(v,32,[128,0,0,0]);put(v,0x3800,[1,2]);v.render_line(0)
      self.assertEqual(pixel(v,7),v.palette[1]);self.assertEqual(pixel(v,0),v.palette[0])
     def test_vertical_flip(self):
      v=VDP();configure(v);put(v,32+7*4,[255,0,0,0]);put(v,0x3800,[1,4]);v.render_line(0);self.assertEqual(pixel(v,0),v.palette[1])
     def test_hscroll_and_top_lock(self):
      v=VDP();configure(v);solid(v,1,1);put(v,0x3800,[1,0]);v.reg[8]=8;v.render_line(0)
      self.assertEqual(pixel(v,8),v.palette[1]);self.assertEqual(pixel(v,0),v.palette[0])
      v.reg[0]|=64;v.render_line(0);self.assertEqual(pixel(v,0),v.palette[1])
     def test_vscroll_frame_latch(self):
      v=VDP();configure(v);solid(v,1,1);put(v,0x3840,[1,0]);v.reg[9]=8;v.render_line(0);self.assertEqual(pixel(v,0),v.palette[0])
      v.tick(228*262);v.render_line(0);self.assertEqual(pixel(v,0),v.palette[1])
     def test_right_scroll_lock(self):
      v=VDP();configure(v);solid(v,1,1);v.yscroll=8;v.reg[0]|=128
      put(v,0x3840+23*2,[1,0,1,0]);v.render_line(0)
      self.assertEqual(pixel(v,184),v.palette[1]);self.assertEqual(pixel(v,192),v.palette[0])
     def test_sprite_first_priority_collision(self):
      v=VDP();configure(v);solid(v,1,1);solid(v,2,2);sprite(v,0,20,0,1);sprite(v,1,20,0,2);v.render_line(0)
      self.assertEqual(pixel(v,20),v.palette[17]);self.assertTrue(v.status&32)
     def test_background_priority_transparent_zero(self):
      v=VDP();configure(v);solid(v,1,1);solid(v,2,2);put(v,0x3800,[1,16,0,16]);sprite(v,0,4,0,2);v.render_line(0)
      self.assertEqual(pixel(v,4),v.palette[1]);self.assertEqual(pixel(v,8),v.palette[18])
     def test_sprite_limit(self):
      v=VDP();configure(v);solid(v,1,1)
      for i in range(9):sprite(v,i,i*8,0,1)
      v.render_line(0);self.assertTrue(v.status&64);self.assertEqual(pixel(v,63),v.palette[17]);self.assertEqual(pixel(v,64),v.palette[0])
     def test_sprite_y_wrap(self):
      v=VDP();configure(v);solid(v,1,1);sprite(v,0,20,-1,1);v.render_line(0);self.assertEqual(pixel(v,20),v.palette[17])
     def test_tall_zoom_sprite(self):
      v=VDP();configure(v);solid(v,2,1);solid(v,3,2);v.reg[1]|=3;sprite(v,0,20,0,3);v.render_line(20)
      self.assertEqual(pixel(v,35,20),v.palette[18]);self.assertEqual(pixel(v,36,20),v.palette[0])
     def test_sprite_bank(self):
      v=VDP();configure(v);solid(v,257,2);v.reg[6]=4;sprite(v,0,20,0,1);v.render_line(0);self.assertEqual(pixel(v,20),v.palette[18])
     def test_blanking_left_mask(self):
      v=VDP();configure(v);solid(v,0,1);v.reg[7]=3;v.reg[0]|=32;v.render_line(0)
      self.assertEqual(pixel(v,0),v.palette[19]);self.assertEqual(pixel(v,8),v.palette[1])
      v.reg[1]=0;v.render_line(0);self.assertEqual(pixel(v,200),v.palette[19])
     def test_modes_report(self):
      v=VDP();self.assertFalse(v.supported);v.reg[0]=6;self.assertTrue(v.supported)
      v.reg[1]=16;self.assertFalse(v.supported);v.reg[1]=8;self.assertFalse(v.supported);v.reg[1]=24;self.assertTrue(v.supported)
     def test_copier_header_all_sizes(self):
      for size in (8192,24576,32768):
       self.assertEqual(GameGear.normalize_rom(bytes(512)+bytes([19])*size),GameGear.normalize_rom(bytes([19])*size))
     def test_partial_control_changes_low_address(self):
      v=VDP();v.control(0);v.control(0x41);v.control(0x23);v.write_data(0xA5);self.assertEqual(v.vram[0x123],0xA5)
     def test_fine_scroll_gap(self):
      v=VDP();configure(v);solid(v,0,1);v.reg[8]=3;v.reg[7]=2;v.render_line(0)
      self.assertEqual(pixel(v,0),v.palette[18]);self.assertEqual(pixel(v,2),v.palette[18]);self.assertEqual(pixel(v,3),v.palette[1])
     def test_fine_scroll_right_lock_boundary(self):
      v=VDP();configure(v);solid(v,1,1);v.yscroll=8;v.reg[8]=3;v.reg[0]|=128
      put(v,0x3840+23*2,[1,0,1,0]);v.render_line(0)
      self.assertEqual(pixel(v,194),v.palette[1]);self.assertEqual(pixel(v,195),v.palette[0])
     def test_invalid_text_mode(self):
      v=VDP();v.reg[0]=4;v.reg[1]=16;self.assertFalse(v.supported)
    class SystemTests(unittest.TestCase):
     def test_controller_active_low_mirrors(self):
      m=GameGear();m.set_keys({'up','right','z'});self.assertEqual(m.in_port(0xDC),0xE6);self.assertEqual(m.in_port(0xC0),0xE6)
      m.release_keys();self.assertEqual(m.in_port(0xDC),255)
     def test_start_port_and_no_reset_button(self):
      m=GameGear();self.assertEqual(m.in_port(0),0xC0);m.set_keys({'return','reset'})
      self.assertEqual(m.in_port(0),0x40);self.assertEqual(m.in_port(0xDD),255)
     def test_native_controller_decode(self):
      m=GameGear();m.set_keys({'z'})
      for p in range(0xC0,256):self.assertEqual(m.in_port(p),0xEF if p in (0xC0,0xDC) else 255)
     def test_disconnected_link_output_reflection(self):
      m=GameGear();m.out_port(2,0);m.out_port(1,0)
      self.assertEqual((m.in_port(1),m.in_port(0xDC),m.in_port(0xDD)),(0,63,112))
      m.out_port(1,128);self.assertEqual(m.in_port(1),128)
      m.out_port(2,255);self.assertEqual(m.in_port(1),255)
     def test_link_readonly_and_stereo(self):
      m=GameGear();m.out_port(4,3);m.out_port(5,255);m.out_port(6,0x31)
      self.assertEqual((m.in_port(4),m.in_port(5),m.in_port(6),m.psg.stereo),(255,248,255,0x31))
     def test_port_decoding(self):
      m=GameGear();m.out_port(0x81,0x37);m.out_port(0xBF,0x88);self.assertEqual(m.vdp.reg[8],0x37)
     def test_psg_latching(self):
      m=GameGear();m.out_port(0x7F,0x85);m.out_port(0x7F,0x12);self.assertEqual(m.psg.reg[0],0x125)
      m.out_port(0x7E,0x9B);self.assertEqual(m.psg.reg[1],11)
     def test_start_does_not_interrupt(self):
      m=GameGear(bytes([0])*32768);m.cpu.halted=True;m.set_keys({'return'});m.run(4)
      self.assertEqual(m.cpu.pc,0);self.assertTrue(m.cpu.halted);self.assertEqual(m.cpu.sp,65535)
     def test_irq_ei_delay(self):
      m=GameGear();m.cpu.pc=0xC000;m.memory[0xC000:0xC003]=b'\xfb\x00\x00';m.vdp.reg[1]=32;m.vdp.frame_pending=True
      m.run(4);self.assertEqual(m.cpu.pc,0xC001);m.run(4);self.assertEqual(m.cpu.pc,0xC002);m.run(1);self.assertEqual(m.cpu.pc,0x38)
     def test_demo_score_target_pause(self):
      m=GameGear();m.run(m.FRAME*10);self.assertEqual(m.memory[0xC007],1)
      m.write(0xC000,m.memory[0xC002]);m.write(0xC001,m.memory[0xC003]);m.run(m.FRAME*2)
      self.assertEqual(m.memory[0xC004],1);self.assertEqual(m.vdp.vram[0x3800+(6*32+19)*2],49)
      m.set_keys({'return','right'});m.run(m.FRAME*3);x=m.memory[0xC000];m.run(m.FRAME*3);self.assertEqual(m.memory[0xC000],x)
      m.release_keys();m.run(m.FRAME*2);m.set_keys({'return','right'});m.run(m.FRAME*3);self.assertGreater(m.memory[0xC000],x)
     def test_demo_x_edge_and_boost(self):
      m=GameGear();m.run(m.FRAME*10);m.set_keys({'x'});m.run(m.FRAME*2);x=m.memory[0xC002];m.run(m.FRAME*5);self.assertEqual(m.memory[0xC002],x)
      m.release_keys();m.run(m.FRAME*2);m.set_keys({'x'});m.run(m.FRAME*2);self.assertNotEqual(m.memory[0xC002],x)
      x=m.memory[0xC000];m.set_keys({'right','z'});m.run(m.FRAME*5);self.assertEqual(m.memory[0xC000]-x,10)
     def test_reset_clears_volatile_ram(self):
      m=GameGear();m.run(m.FRAME*10);m.write(0xC000,123);m.write(0xFFFC,8);m.write(0x8000,234);m.reset()
      self.assertEqual((m.memory[0xC000],m.sram[0],m.cpu.pc),(0,0,0))
     def test_no_files_written_runtime(self):
      from unittest.mock import patch
      with patch('builtins.open',side_effect=AssertionError('unexpected file operation')):
       m=GameGear();m.run(m.FRAME*12);m.reset();m.run(m.FRAME*10)
    class PSGTests(unittest.TestCase):
     def test_stereo_left_right_mute(self):
      import struct
      p=PSG();p.write(0x90);p.stereo=16;p.advance(4096)
      frames=list(struct.iter_unpack('<hh',p.drain()));self.assertTrue(any(l for l,r in frames));self.assertTrue(all(r==0 for l,r in frames))
      p.stereo=1;p.advance(4096);frames=list(struct.iter_unpack('<hh',p.drain()));self.assertTrue(all(l==0 for l,r in frames));self.assertTrue(any(r for l,r in frames))
      p.stereo=0;p.advance(4096);self.assertEqual(set(p.drain()),{0})
     def test_sega_zero_tone_period(self):
      p=PSG();p.clock_tick();self.assertEqual(p.counter[:3],[1,1,1]);self.assertEqual(p.output[:3],[1,1,1])
      p.clock_tick();self.assertEqual(p.output[:3],[0,0,0])
     def test_noise_zero_period_distinct(self):
      p=PSG();p.write(0xE3);p.clock_tick();self.assertEqual(p.counter[3],1)
      p.reg[4]=1;p.clock_tick();self.assertEqual(p.counter[3],2)
     def test_noise_lfsr_and_reset(self):
      p=PSG();p.write(0xE4)
      for i in range(15):p.counter[3]=1;p.clock_tick()
      self.assertEqual(p.lfsr,0x2001);p.write(4);self.assertEqual(p.lfsr,0x8000)
     def test_pcm_bounded_and_drained(self):
      p=PSG();p.advance(p.CLOCK);self.assertLessEqual(len(p.pcm),p.SAMPLE_RATE)
      self.assertLess(abs(p.samples-p.SAMPLE_RATE),2);p.drain();self.assertFalse(p.pcm)
     def test_demo_sound_registers(self):
      m=GameGear();m.run(m.FRAME*10);m.set_keys({'z'});m.run(m.FRAME*2)
      self.assertEqual(m.psg.reg[0],254);self.assertEqual(m.psg.reg[1],6);self.assertEqual(m.psg.stereo,0x90)
      m.write(0xC000,160);m.run(m.FRAME*2);self.assertEqual(m.psg.stereo,9)
      m.release_keys();m.run(m.FRAME*2);self.assertEqual(m.psg.reg[1],15)
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (MemoryTests,VDPTests,SystemTests,PSGTests))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful(): raise SystemExit(1)


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--self-test',action='store_true')
    args=parser.parse_args()
    if args.self_test: self_test(); return
    try: import tkinter as tk
    except ImportError: raise SystemExit('Tkinter is required. Use a Python installation with Tk support.')
    try: root=tk.Tk()
    except tk.TclError as error: raise SystemExit('Tk could not open a desktop display: '+str(error))
    App(root); root.mainloop()

if __name__=='__main__': main()
