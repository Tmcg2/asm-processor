#!/usr/bin/env python3
import argparse
import tempfile
import struct
import copy
import re
import os

EI_NIDENT     = 16
EI_CLASS      = 4
EI_DATA       = 5
EI_VERSION    = 6
EI_OSABI      = 7
EI_ABIVERSION = 8
STN_UNDEF = 0

SHN_UNDEF     = 0
SHN_ABS       = 0xfff1
SHN_COMMON    = 0xfff2
SHN_XINDEX    = 0xffff
SHN_LORESERVE = 0xff00

STT_NOTYPE  = 0
STT_OBJECT  = 1
STT_FUNC    = 2
STT_SECTION = 3
STT_FILE    = 4
STT_COMMON  = 5
STT_TLS     = 6

STB_LOCAL  = 0
STB_GLOBAL = 1
STB_WEAK   = 2

STV_DEFAULT   = 0
STV_INTERNAL  = 1
STV_HIDDEN    = 2
STV_PROTECTED = 3

SHT_NULL          = 0
SHT_PROGBITS      = 1
SHT_SYMTAB        = 2
SHT_STRTAB        = 3
SHT_RELA          = 4
SHT_HASH          = 5
SHT_DYNAMIC       = 6
SHT_NOTE          = 7
SHT_NOBITS        = 8
SHT_REL           = 9
SHT_SHLIB         = 10
SHT_DYNSYM        = 11
SHT_INIT_ARRAY    = 14
SHT_FINI_ARRAY    = 15
SHT_PREINIT_ARRAY = 16
SHT_GROUP         = 17
SHT_SYMTAB_SHNDX  = 18
SHT_MIPS_GPTAB    = 0x70000003
SHT_MIPS_DEBUG    = 0x70000005
SHT_MIPS_REGINFO  = 0x70000006
SHT_MIPS_OPTIONS  = 0x7000000d

SHF_WRITE            = 0x1
SHF_ALLOC            = 0x2
SHF_EXECINSTR        = 0x4
SHF_MERGE            = 0x10
SHF_STRINGS          = 0x20
SHF_INFO_LINK        = 0x40
SHF_LINK_ORDER       = 0x80
SHF_OS_NONCONFORMING = 0x100
SHF_GROUP            = 0x200
SHF_TLS              = 0x400

R_MIPS_32   = 2
R_MIPS_26   = 4
R_MIPS_HI16 = 5
R_MIPS_LO16 = 6


class ElfHeader:
    """
    typedef struct {
        unsigned char   e_ident[EI_NIDENT];
        Elf32_Half      e_type;
        Elf32_Half      e_machine;
        Elf32_Word      e_version;
        Elf32_Addr      e_entry;
        Elf32_Off       e_phoff;
        Elf32_Off       e_shoff;
        Elf32_Word      e_flags;
        Elf32_Half      e_ehsize;
        Elf32_Half      e_phentsize;
        Elf32_Half      e_phnum;
        Elf32_Half      e_shentsize;
        Elf32_Half      e_shnum;
        Elf32_Half      e_shstrndx;
    } Elf32_Ehdr;
    """

    def __init__(self, data):
        self.e_ident = data[:EI_NIDENT]
        self.e_type, self.e_machine, self.e_version, self.e_entry, self.e_phoff, self.e_shoff, self.e_flags, self.e_ehsize, self.e_phentsize, self.e_phnum, self.e_shentsize, self.e_shnum, self.e_shstrndx = struct.unpack('>HHIIIIIHHHHHH', data[EI_NIDENT:])
        assert self.e_ident[EI_CLASS] == 1 # 32-bit
        assert self.e_ident[EI_DATA] == 2 # big-endian
        assert self.e_type == 1 # relocatable
        assert self.e_machine == 8 # MIPS I Architecture
        assert self.e_phoff == 0 # no program header
        assert self.e_shoff != 0 # section header
        assert self.e_shstrndx != SHN_UNDEF

    def to_bin(self):
        return self.e_ident + struct.pack('>HHIIIIIHHHHHH', self.e_type,
                self.e_machine, self.e_version, self.e_entry, self.e_phoff,
                self.e_shoff, self.e_flags, self.e_ehsize, self.e_phentsize,
                self.e_phnum, self.e_shentsize, self.e_shnum, self.e_shstrndx)


class Symbol:
    """
    typedef struct {
        Elf32_Word      st_name;
        Elf32_Addr      st_value;
        Elf32_Word      st_size;
        unsigned char   st_info;
        unsigned char   st_other;
        Elf32_Half      st_shndx;
    } Elf32_Sym;
    """

    def __init__(self, data, strtab):
        self.st_name, self.st_value, self.st_size, st_info, self.st_other, self.st_shndx = struct.unpack('>IIIBBH', data)
        assert self.st_shndx != SHN_XINDEX
        self.bind = st_info >> 4
        self.type = st_info & 15
        self.name = strtab.lookup_str(self.st_name)
        self.visibility = self.st_other & 3

    def to_bin(self):
        st_info = (self.bind << 4) | self.type
        return struct.pack('>IIIBBH', self.st_name, self.st_value, self.st_size, st_info, self.st_other, self.st_shndx)


class Relocation:
    def __init__(self, data, sh_type):
        self.sh_type = sh_type
        if sh_type == SHT_REL:
            self.r_offset, self.r_info = struct.unpack('>II', data)
        else:
            self.r_offset, self.r_info, self.r_addend = struct.unpack('>III', data)
        self.sym_index = self.r_info >> 8
        self.rel_type = self.r_info & 0xff

    def to_bin(self):
        self.r_info = (self.sym_index << 8) | self.rel_type
        if self.sh_type == SHT_REL:
            return struct.pack('>II', self.r_offset, self.r_info)
        else:
            return struct.pack('>III', self.r_offset, self.r_info, self.r_addend)


class Section:
    """
    typedef struct {
        Elf32_Word   sh_name;
        Elf32_Word   sh_type;
        Elf32_Word   sh_flags;
        Elf32_Addr   sh_addr;
        Elf32_Off    sh_offset;
        Elf32_Word   sh_size;
        Elf32_Word   sh_link;
        Elf32_Word   sh_info;
        Elf32_Word   sh_addralign;
        Elf32_Word   sh_entsize;
    } Elf32_Shdr;
    """

    def __init__(self, header, data, index):
        self.sh_name, self.sh_type, self.sh_flags, self.sh_addr, self.sh_offset, self.sh_size, self.sh_link, self.sh_info, self.sh_addralign, self.sh_entsize = struct.unpack('>IIIIIIIIII', header)
        assert not self.sh_flags & SHF_LINK_ORDER
        if self.sh_entsize != 0:
            assert self.sh_size % self.sh_entsize == 0
        if self.sh_type == SHT_NOBITS:
            self.data = ''
        else:
            self.data = data[self.sh_offset:self.sh_offset + self.sh_size]
        self.index = index
        self.relocated_by = []

    @staticmethod
    def from_parts(sh_name, sh_type, sh_flags, sh_link, sh_info, sh_addralign, sh_entsize, data, index):
        header = struct.pack('>IIIIIIIIII', sh_name, sh_type, sh_flags, 0, 0, len(data), sh_link, sh_info, sh_addralign, sh_entsize)
        return Section(header, data, index)

    def lookup_str(self, index):
        assert self.sh_type == SHT_STRTAB
        to = self.data.find(b'\0', index)
        assert to != -1
        return self.data[index:to].decode('utf-8')

    def add_str(self, string):
        assert self.sh_type == SHT_STRTAB
        ret = len(self.data)
        self.data += bytes(string, 'utf-8') + b'\0'
        return ret

    def is_rel(self):
        return self.sh_type == SHT_REL or self.sh_type == SHT_RELA

    def header_to_bin(self):
        if self.sh_type != SHT_NOBITS:
            self.sh_size = len(self.data)
        return struct.pack('>IIIIIIIIII', self.sh_name, self.sh_type, self.sh_flags, self.sh_addr, self.sh_offset, self.sh_size, self.sh_link, self.sh_info, self.sh_addralign, self.sh_entsize)

    def late_init(self, sections):
        if self.sh_type == SHT_SYMTAB:
            self.init_symbols(sections)
        elif self.is_rel():
            self.rel_target = sections[self.sh_info]
            self.rel_target.relocated_by.append(self)
            self.init_relocs()

    def find_symbol(self, name):
        assert self.sh_type == SHT_SYMTAB
        for s in self.symbol_entries:
            if s.name == name:
                return (s.st_shndx, s.st_value)
        return None

    def init_symbols(self, sections):
        assert self.sh_type == SHT_SYMTAB
        assert self.sh_entsize == 16
        self.strtab = sections[self.sh_link]
        entries = []
        for i in range(0, self.sh_size, self.sh_entsize):
            entries.append(Symbol(self.data[i:i+self.sh_entsize], self.strtab))
        self.symbol_entries = entries

    def init_relocs(self):
        assert self.is_rel()
        entries = []
        for i in range(0, self.sh_size, self.sh_entsize):
            entries.append(Relocation(self.data[i:i+self.sh_entsize], self.sh_type))
        self.relocations = entries


class ElfFile:
    def __init__(self, data):
        self.data = data
        assert data[:4] == b'\x7fELF'

        self.elf_header = ElfHeader(data[0:52])

        offset, size = self.elf_header.e_shoff, self.elf_header.e_shentsize
        null_section = Section(data[offset:offset + size], data, 0)
        num_sections = self.elf_header.e_shnum or null_section.sh_size

        self.sections = [null_section]
        for i in range(1, num_sections):
            ind = offset + i * size
            self.sections.append(Section(data[ind:ind + size], data, i))

        symtab = None
        for s in self.sections:
            if s.sh_type == SHT_SYMTAB:
                assert not symtab
                symtab = s
        assert symtab is not None
        self.symtab = symtab

        shstr = self.sections[self.elf_header.e_shstrndx]
        for s in self.sections:
            s.name = shstr.lookup_str(s.sh_name)
            s.late_init(self.sections)

    def find_section(self, name):
        for s in self.sections:
            if s.name == name:
                return s
        return None

    def add_section(self, name, sh_type, sh_flags, sh_link, sh_info, sh_addralign, sh_entsize, data):
        shstr = self.sections[self.elf_header.e_shstrndx]
        sh_name = shstr.add_str(name)
        s = Section.from_parts(sh_name=sh_name, sh_type=sh_type,
                sh_flags=sh_flags, sh_link=sh_link, sh_info=sh_info,
                sh_addralign=sh_addralign, sh_entsize=sh_entsize, data=data,
                index=len(self.sections))
        self.sections.append(s)
        s.name = name
        s.late_init(self.sections)
        return s

    def drop_irrelevant_sections(self):
        # We can only drop sections at the end, since otherwise section
        # references might be wrong. Luckily, these sections typically are.
        while self.sections[-1].sh_type in [SHT_MIPS_DEBUG, SHT_MIPS_GPTAB]:
            self.sections.pop()

    def write(self, filename):
        outfile = open(filename, 'wb')
        outidx = 0
        def write_out(data):
            nonlocal outidx
            outfile.write(data)
            outidx += len(data)
        def pad_out(align):
            if align and outidx % align:
                write_out(b'\0' * (align - outidx % align))

        self.elf_header.e_shnum = len(self.sections)
        write_out(self.elf_header.to_bin())

        for s in self.sections:
            if s.sh_type != SHT_NOBITS and s.sh_type != SHT_NULL:
                pad_out(s.sh_addralign)
                s.sh_offset = outidx
                write_out(s.data)

        pad_out(4)
        self.elf_header.e_shoff = outidx
        for s in self.sections:
            write_out(s.header_to_bin())

        outfile.seek(0)
        outfile.write(self.elf_header.to_bin())
        outfile.close()


def parse_source(f, print_source, optimized):
    if optimized:
        min_instr_count = 2
        skip_instr_count = 1
    else:
        min_instr_count = 4
        skip_instr_count = 4
    SECTIONS = ['.data', '.text', '.rodata', '.late_rodata', '.bss']
    in_asm = False
    instr_count = 0
    fn_section_sizes = None
    fn_ins_inds = None
    asm_conts = []
    late_rodata_asm_conts = None
    first_fn_name = None
    cur_section = None
    start_index = None
    asm_functions = []
    output_lines = []

    # A value that hopefully never appears as a 32-bit rodata constant (or we
    # miscompile late rodata). Increases by 1 in each step.
    cur_late_rodata_hex = 0xE0123456

    namectr = 0
    def make_name(cat):
        nonlocal namectr
        namectr += 1
        return '_asmpp_{}{}'.format(cat, namectr)

    for raw_line in f:
        raw_line = raw_line.rstrip()
        line = raw_line.lstrip()
        output_line = ''
        def instruction():
            nonlocal instr_count
            nonlocal output_line
            assert first_fn_name is not None
            instr_count += 1
            if instr_count > skip_instr_count:
                output_line += '*(volatile int*)0 = 0;'
                return True
            return False

        def add_sized(size):
            assert size % 4 == 0 and size >= 0
            fn_section_sizes[cur_section] += size
            if cur_section == '.text':
                for _ in range(size // 4):
                    instruction()

        if in_asm:
            if line.startswith(')'):
                in_asm = False
                temp_fn_name = None
                if instr_count > 0:
                    temp_fn_name = make_name('func')
                    output_lines[start_index] = 'void {}(void) {{'.format(temp_fn_name)
                    assert first_fn_name
                    assert instr_count >= min_instr_count
                    output_line = '}'
                late_rodata = []
                if fn_section_sizes['.late_rodata'] > 0:
                    # Generate late rodata by emitting unique float constants.
                    # This requires 3 instructions for each 4 bytes of rodata.
                    # Doubles would increase 4 to 8, but unfortunately we know
                    # too little about alignment to be able to use them.
                    size = fn_section_sizes['.late_rodata'] // 4
                    assert size*3 <= len(fn_ins_inds), "late rodata to text ratio is too high: {} / {} must be <= 1/3".format(size, len(fn_ins_inds))
                    for i in range(0, size*3, 3):
                        if (cur_late_rodata_hex & 0xffff) == 0:
                            # Avoid lui
                            cur_late_rodata_hex += 1
                        dummy_bytes = struct.pack('>I', cur_late_rodata_hex)
                        cur_late_rodata_hex += 1
                        late_rodata.append(dummy_bytes)
                        fval, = struct.unpack('>f', dummy_bytes)
                        output_lines[fn_ins_inds[i]] = '*(volatile float*)0 = {}f;'.format(fval)
                        output_lines[fn_ins_inds[i+1]] = ''
                        output_lines[fn_ins_inds[i+2]] = ''
                rodata_name = None
                if fn_section_sizes['.rodata'] > 0:
                    rodata_name = make_name('rodata')
                    output_line += ' const char {}[{}] = {{1}};'.format(rodata_name, fn_section_sizes['.rodata'])
                data_name = None
                if fn_section_sizes['.data'] > 0:
                    data_name = make_name('data')
                    output_line += ' char {}[{}] = {{1}};'.format(data_name, fn_section_sizes['.data'])
                asm_functions.append((first_fn_name, asm_conts, late_rodata, late_rodata_asm_conts, {
                    '.text': (temp_fn_name, fn_section_sizes['.text']),
                    '.data': (data_name, fn_section_sizes['.data']),
                    '.rodata': (rodata_name, fn_section_sizes['.rodata']),
                }))
            else:
                line = re.sub(r'/\*.*?\*/', '', line)
                line = re.sub(r'#.*', '', line)
                line = line.strip()
                changed_section = False
                if line.startswith('glabel ') and first_fn_name is None and cur_section == '.text':
                    first_fn_name = line.split()[1]
                if not line:
                    pass # empty line
                elif line.startswith('glabel ') or (line.startswith('.') and line.endswith(':')):
                    pass # label
                elif line.startswith('.section') or line in ['.text', '.data', '.rdata', '.rodata', '.late_rodata']:
                    # section change
                    cur_section = '.rodata' if line == '.rdata' else line.split(',')[0].split()[-1]
                    changed_section = True
                    assert cur_section in SECTIONS, "unrecognized .section directive"
                    assert cur_section != '.bss' "bss sections not supported yet"
                elif line.startswith('.incbin'):
                    add_sized(int(line.split(',')[-1].strip(), 0))
                elif line.startswith('.word') or line.startswith('.float'):
                    add_sized(4 * len(line.split(',')))
                elif line.startswith('.'):
                    # .macro, .ascii, .balign, ...
                    assert False, 'not supported yet: ' + line
                else:
                    # Unfortunately, macros are hard to support for .rodata --
                    # we don't know how how space they will expand to before
                    # running the assembler, but we need that information to
                    # construct the C code. So if we need that we'll either
                    # need to run the assembler twice (at least in some rare
                    # cases), or change how this program is invoked.
                    # Similarly, we can't currently deal with pseudo-instructions
                    # that expand to several real instructions.
                    assert cur_section == '.text', "instruction or macro call in non-.text section? not supported: " + line
                    fn_section_sizes['.text'] += 4
                    if instruction():
                        fn_ins_inds.append(len(output_lines))
                if cur_section == '.late_rodata':
                    if not changed_section:
                        late_rodata_asm_conts.append(line)
                else:
                    asm_conts.append(line)
        else:
            if line.startswith('GLOBAL_ASM('):
                in_asm = True
                cur_section = '.text'
                instr_count = 0
                asm_conts = []
                late_rodata_asm_conts = []
                start_index = len(output_lines)
                first_fn_name = None
                fn_section_sizes = {
                    '.text': 0,
                    '.data': 0,
                    '.rodata': 0,
                    '.late_rodata': 0,
                }
                fn_ins_inds = []
            else:
                output_line = raw_line

        # Print exactly one output line per source line, to make compiler
        # errors have correct line numbers.
        output_lines.append(output_line)

    if print_source:
        for line in output_lines:
            print(line)

    return asm_functions

def fixup_objfile(objfile_name, functions, asm_prelude, assembler):
    SECTIONS = ['.data', '.text', '.rodata']

    with open(objfile_name, 'rb') as f:
        objfile = ElfFile(f.read())

    prev_locs = {
        '.text': 0,
        '.data': 0,
        '.rodata': 0,
    }
    to_copy = {
        '.text': [],
        '.data': [],
        '.rodata': [],
    }
    asm = []
    late_rodata = []
    late_rodata_asm = []
    late_rodata_source_name = None

    # Generate an assembly file with all the assembly we need to fill in. For
    # simplicity we pad with nops/.space so that addresses match exactly, so we
    # don't have to fix up relocations/symbol references.
    temp_names = set()
    first_fn_names = set()
    for (first_fn_name, body, fn_late_rodata, fn_late_rodata_body, data) in functions:
        ifdefed = False
        for sectype, (temp_name, size) in data.items():
            if temp_name is None:
                continue
            assert size > 0
            temp_names.add(temp_name)
            loc = objfile.symtab.find_symbol(temp_name)
            if loc is None:
                ifdefed = True
                break
            loc = loc[1]
            prev_loc = prev_locs[sectype]
            assert loc >= prev_loc
            if loc != prev_loc:
                asm.append('.section ' + sectype)
                if sectype == '.text':
                    for i in range((loc - prev_loc) // 4):
                        asm.append('nop')
                else:
                    asm.append('.space {}'.format(loc - prev_loc))
            to_copy[sectype].append((loc, size))
            prev_locs[sectype] = loc + size
        if not ifdefed:
            if first_fn_name:
                first_fn_names.add(first_fn_name)
            late_rodata.extend(fn_late_rodata)
            late_rodata_asm.extend(fn_late_rodata_body)
            asm.append('.text')
            for line in body:
                asm.append(line)
    if late_rodata_asm:
        late_rodata_source_name = '_asmpp_late_rodata'
        temp_names.add(late_rodata_source_name)
        asm.append('.rdata')
        asm.append('glabel {}'.format(late_rodata_source_name))
        asm.extend(late_rodata_asm)

    o_file = tempfile.NamedTemporaryFile(prefix='asm-processor', suffix='.o', delete=False)
    o_name = o_file.name
    o_file.close()
    s_file = tempfile.NamedTemporaryFile(prefix='asm-processor', suffix='.s', delete=False)
    s_name = s_file.name
    try:
        s_file.write(asm_prelude + b'\n')
        for line in asm:
            s_file.write(line.encode('utf-8') + b'\n')
        s_file.close()
        ret = os.system(assembler + " " + s_name + " -o " + o_name)
        if ret != 0:
            raise Exception("failed to assemble")
        with open(o_name, 'rb') as f:
            asm_objfile = ElfFile(f.read())

        # Remove some clutter from objdump output
        objfile.drop_irrelevant_sections()

        # Unify reginfo sections
        target_reginfo = objfile.find_section('.reginfo')
        source_reginfo_data = list(asm_objfile.find_section('.reginfo').data)
        data = list(target_reginfo.data)
        for i in range(20):
            data[i] |= source_reginfo_data[i]
        target_reginfo.data = bytes(data)

        # Move over section contents
        modified_text_positions = set()
        last_rodata_pos = 0
        for sectype in SECTIONS:
            source = asm_objfile.find_section(sectype)
            target = objfile.find_section(sectype)
            if source is None or not to_copy[sectype]:
                continue
            assert target is not None, "must have a section to overwrite: " + sectype
            data = list(target.data)
            for (pos, count) in to_copy[sectype]:
                data[pos:pos + count] = source.data[pos:pos + count]
                if sectype == '.text':
                    assert count % 4 == 0
                    assert pos % 4 == 0
                    for i in range(count // 4):
                        modified_text_positions.add(pos + 4 * i)
                elif sectype == '.rodata':
                    last_rodata_pos = pos + count
            target.data = bytes(data)

        # Move over late rodata. This is heuristic, sadly, since I can't think
        # of another way of doing it.
        moved_late_rodata = {}
        if late_rodata:
            source = asm_objfile.find_section('.rodata')
            target = objfile.find_section('.rodata')
            source_pos = asm_objfile.symtab.find_symbol(late_rodata_source_name)
            assert source_pos is not None and source_pos[0] == source.index
            source_pos = source_pos[1]
            new_data = list(target.data)
            for dummy_bytes in late_rodata:
                pos = target.data.index(dummy_bytes, last_rodata_pos)
                new_data[pos:pos+4] = source.data[source_pos:source_pos+4]
                moved_late_rodata[source_pos] = pos
                last_rodata_pos = pos + 4
                source_pos += 4
            target.data = bytes(new_data)

        # Merge strtab data.
        strtab_adj = len(objfile.symtab.strtab.data)
        objfile.symtab.strtab.data += asm_objfile.symtab.strtab.data

        # Move over symbols, deleting the temporary function labels.
        # Sometimes this naive procedure results in duplicate symbols, or UNDEF
        # symbols that are also defined the same .o file. Hopefully that's fine.
        # Skip over local symbols -- we don't need them, and they would need to
        # be reordered before all the existing global ones.
        new_entries = []
        index = 0
        for s in objfile.symtab.symbol_entries:
            if s.name not in temp_names:
                s.new_index = index
                index += 1
                new_entries.append(s)
        num_local_syms = asm_objfile.symtab.sh_info
        for s in asm_objfile.symtab.symbol_entries[num_local_syms:]:
            if s.st_shndx != SHN_UNDEF:
                section_name = asm_objfile.sections[s.st_shndx].name
                assert section_name in SECTIONS, "Generated assembly .o must only have symbols for .text, .data, .rodata and UNDEF, but found {}".format(section_name)
                s.st_shndx = objfile.find_section(section_name).index
                # glabel's aren't marked as functions, making objdump output confusing. Fix that.
                if s.name in first_fn_names:
                    s.type = STT_FUNC
            if s.name in temp_names:
                continue
            if objfile.sections[s.st_shndx].name == '.rodata' and s.st_value in moved_late_rodata:
                s.st_value = moved_late_rodata[s.st_value]
            s.st_name += strtab_adj
            s.new_index = index
            index += 1
            new_entries.append(s)
        objfile.symtab.data = b''.join(s.to_bin() for s in new_entries)

        # Move over relocations
        for sectype in SECTIONS:
            source = asm_objfile.find_section(sectype)
            target = objfile.find_section(sectype)

            if target is not None:
                # fixup relocation symbol indices, since we butchered them above
                for reltab in target.relocated_by:
                    nrels = []
                    for rel in reltab.relocations:
                        if sectype == '.text' and rel.r_offset in modified_text_positions:
                            # don't include relocations for late_rodata dummy code
                            continue
                        # hopefully we don't have relocations for local or
                        # temporary symbols, so new_index exists
                        rel.sym_index = objfile.symtab.symbol_entries[rel.sym_index].new_index
                        nrels.append(rel)
                    reltab.relocations = nrels
                    reltab.data = b''.join(rel.to_bin() for rel in nrels)

            if not source:
                continue

            target_reltab = objfile.find_section('.rel' + sectype)
            target_reltaba = objfile.find_section('.rela' + sectype)
            for reltab in source.relocated_by:
                for rel in reltab.relocations:
                    assert rel.sym_index >= num_local_syms, "Must only have relocations pointing to global symbols"
                    rel.sym_index = asm_objfile.symtab.symbol_entries[rel.sym_index].new_index
                    if sectype == '.rodata' and rel.r_offset in moved_late_rodata:
                        rel.r_offset = moved_late_rodata[rel.r_offset]
                new_data = b''.join(rel.to_bin() for rel in reltab.relocations)
                if reltab.sh_type == SHT_REL:
                    if not target_reltab:
                        target_reltab = objfile.add_section('.rel' + sectype,
                                sh_type=SHT_REL, sh_flags=0,
                                sh_link=objfile.symtab.index, sh_info=target.index,
                                sh_addralign=4, sh_entsize=8, data=b'')
                    target_reltab.data += new_data
                else:
                    if not target_reltaba:
                        target_reltaba = objfile.add_section('.rela' + sectype,
                                sh_type=SHT_RELA, sh_flags=0,
                                sh_link=objfile.symtab.index, sh_info=target.index,
                                sh_addralign=4, sh_entsize=12, data=b'')
                    target_reltaba.data += new_data

        objfile.write(objfile_name)
    finally:
        s_file.close()
        os.remove(s_name)
        try:
            os.remove(o_name)
        except:
            pass

def main():
    parser = argparse.ArgumentParser(description="Pre-process .c files and post-process .o files to enable embedding assembly into C.")
    parser.add_argument('filename', help="path to .c code")
    parser.add_argument('--post-process', dest='objfile', help="path to .o file to post-process")
    parser.add_argument('--assembler', dest='assembler', help="assembler command (e.g. \"mips-linux-gnu-as -march=vr4300 -mabi=32\")")
    parser.add_argument('--asm-prelude', dest='asm_prelude', help="path to a file containing a prelude to the assembly file (with .set and .macro directives, e.g.)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('-O2', dest='optimized', action='store_true')
    group.add_argument('-g', dest='optimized', action='store_false')
    args = parser.parse_args()

    if args.objfile is None:
        with open(args.filename) as f:
            parse_source(f, print_source=True, optimized=args.optimized)
    else:
        assert args.assembler is not None, "must pass assembler command"
        with open(args.filename) as f:
            functions = parse_source(f, print_source=False, optimized=args.optimized)
        if not functions:
            return
        asm_prelude = b''
        if args.asm_prelude:
            with open(args.asm_prelude, 'rb') as f:
                asm_prelude = f.read()
        fixup_objfile(args.objfile, functions, asm_prelude, args.assembler)

if __name__ == "__main__":
    main()
