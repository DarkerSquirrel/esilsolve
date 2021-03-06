from esilsolve import ESILSolver
import angr
import claripy
import z3
import r2pipe
from binascii import hexlify, unhexlify
import re
import logging
import archinfo

logging.getLogger('angr').setLevel('ERROR')
reg_pattern = re.compile('^reg_([a-z0-9_]+)_\\d+_\\d+$')
mem_pattern = re.compile('^mem_([a-f0-9]+)_\\d+_(\\d+)$')

arch_dict = {
    64: {"arm": "aarch64", "x86": "amd64", "ppc": "ppc64", "mips": "mips64"}
}

archinfo_dict = {
    "x86": archinfo.ArchX86,
    "arm": archinfo.ArchARM,
    "aarch64": archinfo.ArchAArch64,
    "amd64": archinfo.ArchAMD64,
    "mips": archinfo.ArchMIPS32,
    "mips64": archinfo.ArchMIPS64,
    "ppc": archinfo.ArchPPC32,
    "ppc64": archinfo.ArchPPC64
}

# the whole idea of this script is in jeopardy
# since the flags do not seem to get set properly by angr
# it could be because of the "lazy" setting but i dont know
# the results don't seem consistent but esil is not wrong
class ESILCheck:
    def __init__(self, arch, bits=64):
        self.arch = arch
        self.bits = bits

        self.aarch = self.arch
        if bits in arch_dict and arch in arch_dict[bits]:
            self.aarch = arch_dict[bits][arch]

        self.converter = claripy.backends.z3
        self.r2p = r2p = r2pipe.open("-", ["-a", self.arch, "-b", str(self.bits), "-2"])

    def check(self, instruction=None, code=None, esil=None, check_flags=True):
        r2p = self.r2p 

        if instruction == None:
            r2p.cmd("wx %s" % hexlify(code).decode())
        else:
            r2p.cmd("wa %s" % instruction)

        instr = r2p.cmdj("pdj 1")[0]

        if esil != None:
            instr["esil"] = esil

        code = unhexlify(instr["bytes"])
        if all([x == 0 for x in code]):
            print("[!] failed to assemble instruction")
            return 

        print("[*] instruction: %s : %s\n" % (instr["opcode"], instr["esil"]))

        basesolver = self.converter.solver()

        esilsolver = ESILSolver(r2p, sym=True)
        esstate = esilsolver.blank_state()
        esstate.memory[0] = code # oof            

        esclone = esstate.clone()

        proj = angr.load_shellcode(code, arch=archinfo_dict[self.aarch]())
        state = proj.factory.blank_state()

        if self.arch == "arm":
            basesolver.add(
                esstate.registers["sp"] == self.converter.convert(state.regs.sp))

        block = proj.factory.block(proj.entry)

        successor = state.step()[0]

        essuccessor = esclone.proc.execute_instruction(esclone, instr)[0]

        #basesolver.add(esstate.registers["ebx"] != 2147483648)
        #basesolver.add(esstate.registers["ebx"] != 0)

        insn = block.capstone.insns[0].insn
        regs_read, regs_write = insn.regs_access()
        equivalent = True

        print("[-] read: ")
        for reg in regs_read:
            regn = insn.reg_name(reg)

            try:
                regv = getattr(successor.regs, regn)
                esregv = essuccessor.registers[regn]
                convregv = self.converter.convert(regv)
                #basesolver.add(convregv > 0)
                print("[+]\tangr %s: %s" % (regn, trunc(regv)),)
                print("[+]\t ES  %s: %s" % (regn, trunc(esregv)),)
            except Exception as e:
                print("[!] error with read reg %s: %s" % (regn, str(e)))
        
        print("\n[-] write: ")
        for reg in regs_write:
            basesolver.push()
            equated = {}

            regn = insn.reg_name(reg)
            try:
            #if True:
                esregv = prepare(essuccessor.registers[regn])

                if essuccessor.registers._registers[regn]["type_str"] == "flg" and not check_flags:
                    continue
                
                regv = getattr(successor.regs, regn)
                convregv = prepare(self.converter.convert(regv))
                #basesolver.add(convregv > 0)
                basesolver.add(esregv != convregv)

                print("[+]\tangr %s: %s" % (regn, trunc(regv)),)
                print("[+]\t ES  %s: %s" % (regn, trunc(esregv)),)

                self.equate_regs(basesolver, convregv, esstate, essuccessor, equated)

                #if "flags" in regn:

                #    basesolver.add(convregv & 0x4 == 0)
                #    basesolver.add(convregv & 0x10 == 0)
                #    basesolver.add(esregv & 0x10 == 0)

            except Exception as e:
                print("[!] error with write reg %s: %s" % (regn, str(e)))
                continue

            #basesolver.add(essuccessor.registers["af"] == 1)
            satisfiable = basesolver.check()
            if satisfiable == z3.sat:
                equivalent = False
                model = basesolver.model()
                print("[!]\tunequal model: %s" % str(model).replace("\n", ""))
                print("[*]\t\tangr %s: %x" % (regn, model.eval(convregv).as_long()))
                print("[*]\t\t ES  %s: %x\n" % (regn, model.eval(esregv).as_long()))

            else:
                print("[*]\timplementations are equivalent!\n")
            
            basesolver.pop()

        return equivalent

    def equate_regs(self, basesolver, angrstmt, esstate, essuccessor, equated, depth=0):
        esreg = self.stmt_to_reg(angrstmt)
        if esreg != None:
            if esreg not in equated:
                if "cc_dep" in esreg: # previous vex flag placeholder, zero it
                    basesolver.add(angrstmt == 0)
                else:
                    basesolver.add(angrstmt == esstate.registers[esreg])

                equated[esreg] = True
        else:
            esmem = self.stmt_to_mem(angrstmt)
            if esmem != None:
                esmemname = "mem_%016x" % esmem["addr"]
                if esmemname not in equated:
                    # jesus this script has some bad hacks in it
                    esaddr = list(essuccessor.memory._memory.keys())[0]
                    esdata = essuccessor.memory.read_bv(esaddr, int(esmem["size"]/8))
                    basesolver.add(angrstmt == esdata)
                    equated[esmemname] = True
            else:
                for child in angrstmt.children():
                    #print("[*]%schild: %s" % ("\t"*(depth+2),trunc(child)))
                    self.equate_regs(basesolver, child, esstate, essuccessor, equated, depth+1)

    # this is terrible but its what I have until I find out 
    # how to do the conversion properly, if it is even possible
    def stmt_to_reg(self, stmt):
        stmt_str = str(stmt)
        matches = re.search(reg_pattern, stmt_str)

        if matches == None:
            return
        else:
            return matches.group(1)

    def stmt_to_mem(self, stmt):
        stmt_str = str(stmt)
        matches = re.search(mem_pattern, stmt_str)

        if matches == None:
            return
        else:
            return {
                "addr": int(matches.group(1), 16),
                "size": int(matches.group(2))
            }

SIZE = 64
def prepare(val):
    if z3.is_bv(val):
        #print(val)
        szdiff = SIZE-val.size()
        #print(szdiff, val.size())
        if szdiff > 0:
            return z3.ZeroExt(szdiff, val)
        else:
            return val
    elif z3.is_int(val):
        return z3.Int2BV(val, SIZE)
    else:
        return z3.BitVecVal(val, SIZE)

def trunc(s, maxlen=64):
    s = str(s).replace("\n", " ")
    if len(s) > maxlen:
        return s[:maxlen] + "..."
    else:
        return s

if __name__ == "__main__":

    esilcheck = ESILCheck("ppc", bits=32)
    #esilcheck.check("or eax, ebx")
    #esilcheck.check(code=b"\x48\x63\xff")
    esilcheck.check(code=unhexlify("7c290b78")) #"sub rax, rbx") 
    exit()
    esilcheck.check("imul eax, edx") 
    esilcheck.check("imul ebx") # edx not equivalent
    exit()

    esilcheck = ESILCheck("arm", bits=64)
    esilcheck.check(code=unhexlify("dac020cb"))
    #esilcheck.check("movn w0, w1")
    exit()
    esilcheck.check("cmp r0, r1")

    esilcheck = ESILCheck("arm", bits=64)
    esilcheck.check("add x0, x0, x1")
    esilcheck.check(code=b"\x84\x10\xc0\xda")

    exit()

    esilcheck = ESILCheck("amd64", bits=64)
    esilcheck.check("add rax, rbx")
    esilcheck.check("sub rax, rbx")

    esilcheck = ESILCheck("x86", bits=32)
    esilcheck.check("add eax, [ebx]")
    esilcheck.check("sub eax, [ebx]") 