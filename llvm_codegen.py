import sys

try:
    import llvm.core
    import llvm.ee
    import llvm.passes
except ImportError:
    llvm = None

import codegen
import minierror
import minitypes
import minivisitor
import ctypes_conversion

class LLVMCodeGen(codegen.CodeGen):

    in_lhs_expr = 0

    def __init__(self, context, codewriter):
        super(LLVMCodeGen, self).__init__(context, codewriter)
        self.declared_temps = set()
        self.temp_names = set()

        self.astbuilder = context.astbuilder
        self.blocks = {}
        self.symtab = {}
        self.llvm_temps = {}

        import llvm # raise an error at this point if llvm-py is not installed

        self.init_binops()
        self.init_comparisons()

    def append_basic_block(self, name='unamed'):
        idx = len(self.blocks)
        bb = self.lfunc.append_basic_block('%s_%d' % (name, idx))
        self.blocks[idx] = bb
        return bb

    def visit_FunctionNode(self, node):
        self.specializer = node.specializer
        self.function = node
        self.llvm_module = self.context.llvm_module

        name = node.name + node.specialization_name
        node.mangled_name = name

        lfunc_type = node.type.to_llvm(self.context)
        self.lfunc = self.llvm_module.add_function(lfunc_type, node.mangled_name)

        entry = self.append_basic_block('entry')
        self.builder = llvm.core.Builder.new(entry)

        self.add_arguments(node)
        self.visit(node.body)

        # self.context.llvm_fpm.run(self.lfunc)
        self.code.write(self.lfunc)
        ctypes_func = ctypes_conversion.get_ctypes_func(
                    node, self.lfunc, self.context.llvm_ee, self.context)
        self.code.write(ctypes_func)

    def add_arguments(self, function):
        i = 0
        for arg in function.arguments + function.scalar_arguments:
            for var in arg.variables:
                self.symtab[var.name] = self.lfunc.args[i]
                i += 1

    def visit_PrintNode(self, node):
        pass

    def visit_Node(self, node):
        self.visitchildren(node)
        return node

    def visit_OpenMPConditionalNode(self, node):
        if node.else_body:
            self.visit(node.else_body)
        return node

    def visit_ForNode(self, node):
        '''
        Implements simple for loops with iternode as range, xrange
        '''
        bb_cond = self.append_basic_block('for.cond')
        bb_incr = self.append_basic_block('for.incr')
        bb_body = self.append_basic_block('for.body')
        bb_exit = self.append_basic_block('for.exit')

        # generate initializer
        self.visit(node.init)
        self.builder.branch(bb_cond)

        # generate condition
        self.builder.position_at_end(bb_cond)
        cond = self.visit(node.condition)
        self.builder.cbranch(cond, bb_body, bb_exit)

        # generate increment
        self.builder.position_at_end(bb_incr)
        self.visit(node.step)
        self.builder.branch(bb_cond)

        # generate body
        self.builder.position_at_end(bb_body)
        self.visit(node.body)
        self.builder.branch(bb_incr)

        # move to exit block
        self.builder.position_at_end(bb_exit)

    def visit_IfNode(self, node):
        cond = self.visit(node.cond)

        bb_true = self.append_basic_block('if.true')
        bb_endif = self.append_basic_block('if.end')

        if node.else_body:
            bb_false = self.append_basic_block('if.false')
        else:
            bb_false = bb_endif

        self.builder.cbranch(test, bb_true, bb_false)

        # if cond
        self.builder.position_at_end(bb_true)
        self.visit(node.body)
        self.builder.branch(bb_endif)

        if node.else_body:
            # else
            self.builder.position_at_end(bb_false)
            self.visit(node.else_body)
            self.builder.branch(bb_endif)

        # endif
        self.builder.position_at_end(bb_endif)

    def visit_ReturnNode(self, node):
        self.builder.ret(self.visit(node.operand))

    def visit_CastNode(self, node):
        result = self.visit(node.operand)
        if node.type.is_pointer:
            return result.bitcast(node.type)

        return self.visit_PromotionNode(node)

    def visit_PromotionNode(self, node):
        result = self.visit(node.operand)
        type = node.type
        op_type = node.operand.type

        smaller = type.itemsize < op_type.itemsize
        if type.is_int and op_type.is_int:
            op = (('zext', 'sext'), ('trunc', 'trunc'))[smaller][type.signed]
        elif type.is_float and op_type.is_float:
            op =  ('fpext', 'fptrunc')[smaller]
        elif type.is_int and op_type.is_float:
            op = ('fptoui', 'fptosi')[type.signed]
        elif type.is_float and op_type.is_int:
            op = ('fptoui', 'fptosi')[type.signed]
        else:
            raise NotImplementedError((type, op_type))

        ltype = type.to_llvm(self.context)
        return getattr(self.builder, op)(result, ltype)

    def init_binops(self):
        # (float_op, unsigned_int_op, signed_int_op)
        self._binops = {
            '+': ('fadd', 'add', 'add'),
            '-': ('fsub', 'sub', 'sub'),
            '*': ('fmul', 'mul', 'mul'),
            '/': ('fdiv', 'udiv', 'sdiv'),
            '%': ('frem', 'urem', 'srem'),

            '&': (None, 'and_', 'and_'),
            '|': (None, 'or_', 'or_'),
            '^': (None, 'xor', 'xor'),

            # TODO: other ops
        }

    def init_comparisons(self):
        self._compare_mapping_float = {
            '>':  llvm.core.FCMP_OGT,
            '<':  llvm.core.FCMP_OLT,
            '==': llvm.core.FCMP_OEQ,
            '>=': llvm.core.FCMP_OGE,
            '<=': llvm.core.FCMP_OLE,
            '!=': llvm.core.FCMP_ONE,
        }

        self._compare_mapping_sint = {
            '>':  llvm.core.ICMP_SGT,
            '<':  llvm.core.ICMP_SLT,
            '==': llvm.core.ICMP_EQ,
            '>=': llvm.core.ICMP_SGE,
            '<=': llvm.core.ICMP_SLE,
            '!=': llvm.core.ICMP_NE,
        }

        self._compare_mapping_uint = {
            '>':  llvm.core.ICMP_UGT,
            '<':  llvm.core.ICMP_ULT,
            '==': llvm.core.ICMP_EQ,
            '>=': llvm.core.ICMP_UGE,
            '<=': llvm.core.ICMP_ULE,
            '!=': llvm.core.ICMP_NE,
        }

    def visit_BinopNode(self, node):
        lhs = self.visit(node.lhs)
        rhs = self.visit(node.rhs)

        op = node.operator
        if (node.type.is_int or node.type.is_float) and node.operator in self._binops:
            llvm_method_name = self._binops[op][node.type.is_int + node.type.is_signed]
            meth = getattr(self.builder, llvm_method_name)
            if lhs.type != rhs.type:
                node.print_tree(self.context)
                assert False, (node.lhs.type, node.rhs.type, lhs.type, rhs.type)
            return meth(lhs, rhs)
        elif node.operator in self._compare_mapping_float:
            return self.generate_compare(node, op, lhs, rhs)
        elif node.type.is_pointer:
            if node.rhs.type.is_pointer:
                lhs, rhs = rhs, lhs
            return self.builder.gep(lhs, [rhs])
        else:
            raise minierror.CompileError(
                node, "Binop %s (type=%s) not implemented for types (%s, %s)" % (
                                                op, node.type, lhs.type, rhs.type))

    def generate_compare(self, node, op, lhs_value, rhs_value):
        op = node.operator
        lop = None

        if node.lhs.type.is_float and node.rhs.type.is_float:
            lfunc = self.builder.fcmp
            lop = self._compare_mapping_float[op]
        elif node.lhs.type.is_int and node.rhs.type.is_int:
            lfunc = self.builder.icmp
            if node.lhs.type.signed and node.rhs.type.signed:
                lop = self._compare_mapping_sint[op]
            elif not (node.lhs.type.signed or node.rhs.type.signed):
                lop = self._compare_mapping_uint[op]

        if lop is None:
            raise minierror.CompileError(
                node, "%s for types (%s, %s)" % (node.operator,
                                                 node.lhs.type, node.rhs.type))

        return lfunc(lop, lhs_value, rhs_value)

    def visit_UnopNode(self, node):
        result = self.visit(node.operand)
        if node.operator == '-':
            return self.builder.neg(result)
        elif node.operator == '+':
            return result
        else:
            raise NotImplementedError(node.operator)

    def visit_TempNode(self, node):
        if node not in self.declared_temps:
            llvm_temp = self._declare_temp(node)
        else:
            llvm_temp = self.llvm_temps[node]

        if self.in_lhs_expr:
            return llvm_temp
        else:
            return self.builder.load(llvm_temp)

    def _mangle_temp(self, node):
        name = node.repr_name or node.name
        if name in self.temp_names:
            name = "%s%d" % (name, len(self.declared_temps))
        node.name = name
        self.temp_names.add(name)
        self.declared_temps.add(node)

    def _declare_temp(self, node, rhs_result=None):
        if node not in self.declared_temps:
            self._mangle_temp(node)

        llvm_temp = self.builder.alloca(node.type.to_llvm(self.context),
                                        node.name)
        self.llvm_temps[node] = llvm_temp
        return llvm_temp

    def visit_AssignmentExpr(self, node):
        self.in_lhs_expr += 1
        lhs = self.visit(node.lhs)
        self.in_lhs_expr -= 1
        rhs = self.visit(node.rhs)
        return self.builder.store(rhs, lhs)

    def visit_SingleIndexNode(self, node):
        in_lhs_expr = self.in_lhs_expr
        if in_lhs_expr:
            self.in_lhs_expr -= 1
        lhs = self.visit(node.lhs)
        rhs = self.visit(node.rhs)
        if in_lhs_expr:
            self.in_lhs_expr += 1

        result = self.builder.gep(lhs, [rhs])

        if self.in_lhs_expr:
            return result
        else:
           return self.builder.load(result)

    def visit_DereferenceNode(self, node):
        node = self.astbuilder.index(node.operand, self.astbuilder.constant(0))
        return self.visit_SingleIndexNode(node)

    def visit_SizeofNode(self, node):
        return self.visit(self.astbuilder.constant(node.type.itemsize))

    def visit_Variable(self, node):
        value = self.symtab[node.name]
        return value
        if self.in_lhs_expr:
            return value
        else:
            return self.builder.load(value)

    def visit_ArrayAttribute(self, node):
        return self.symtab[node.name]

    def visit_NoopExpr(self, node):
        pass

    def visit_ResolvedVariable(self, node):
        return self.visit(node.element)

    def visit_JumpNode(self, node):
        return self.builder.branch(self.visit(node.label))

    def visit_JumpTargetNode(self, node):
        basic_block = self.visit(node.label)
        self.builder.branch(basic_block)
        self.builder.position_at_end(basic_block)

    def visit_LabelNode(self, node):
        if node not in self.labels:
            self.labels[node] = self.append_basic_block(node.label)

        return self.labels[node]

    def handle_string_constant(self, b, constant):
        #lchar = minitypes.char.to_llvm(self.context)
        #ltype = llvm.core.Type.array(lchar, len(constant) + 1)
        string_constants = self.context.string_constants = getattr(
                                    self.context, 'string_constants', {})
        if constant in string_constants:
            lvalue = string_constants[constant]
        else:
            lstring = llvm.core.Constant.stringz(constant)
            lvalue = self.context.llvm_module.add_global_variable(
                        lstring.type, "__string_%d" % len(string_constants))
            lvalue.initializer = lstring
            lvalue.linkage = llvm.core.LINKAGE_INTERNAL

            lzero = self.visit(b.constant(0))
            lvalue = self.builder.gep(lvalue, [lzero, lzero])
            string_constants[constant] = lvalue

        return lvalue

    def visit_ConstantNode(self, node):
        b = self.astbuilder

        ltype = node.type.to_llvm(self.context)
        constant = node.value

        if node.type.is_float:
            lvalue = llvm.core.Constant.real(ltype, constant)
        elif node.type.is_int:
            lvalue = llvm.core.Constant.int(ltype, constant)
        elif node.type.is_pointer and self.pyval == 0:
            lvalue = llvm.core.ConstantPointerNull
        elif node.type.is_c_string:
            lvalue = self.handle_string_constant(b, constant)
        else:
            raise NotImplementedError("Constant %s of type %s" % (constant,
                                                                  node.type))

        return lvalue

    def visit_FuncCallNode(self, node):
        llvm_args = self.results(node.args)
        llvm_func = self.visit(node.func_or_pointer)
        return self.builder.call(llvm_func, llvm_args)

    def visit_FuncNameNode(self, node):
        try:
            printf = self.context.llvm_module.get_function_named('printf')
        except llvm.LLVMException:
            func_type = node.type.to_llvm(self.context)
            printf = self.context.llvm_module.add_function(func_type, node.name)

        return printf

    def visit_FuncRefNode(self, node):
        raise NotImplementedError