"""
Specializers for various sorts of data layouts and memory alignments.
"""

import copy

import minivisitor
import miniutils
import minitypes

class ASTMapper(minivisitor.VisitorTransform):

    def __init__(self, context):
        super(ASTMapper, self).__init__(context)
        self.astbuilder = context.astbuilder

    def getpos(self, opaque_node):
        return self.context.getpos(opaque_node)

    def map_type(self, opaque_node, **kwds):
        return self.context.typemapper.map_type(
                        self.context.gettype(opaque_node), **kwds)

    def visit(self, node, *args):
        prev = self.astbuilder.pos
        self.astbuilder.pos = node.pos
        result = super(ASTMapper, self).visit(node)
        self.astbuilder.pos = prev
        return result

class Specializer(ASTMapper):
    """
    Implement visit_* methods to specialize to some pattern. The default is
    to copy the node and specialize the children.
    """

    is_contig_specializer = False
    is_tiled_specializer = False

    def __init__(self, context, specialization_name=None):
        super(Specializer, self).__init__(context)
        if specialization_name is not None:
            self.specialization_name = specialization_name

        self.variables = {}
        self.error_handlers = []

    def getpos(self, node):
        return node.pos

    def visit(self, node, *args):
        result = super(Specializer, self).visit(node)
        if result is not None:
            result.is_specialized = True
        return result

    def visit_Node(self, node):
        # node = copy.copy(node)
        self.visitchildren(node)
        return node

    def _index_list(self, pointer, ndim):
        return [self.astbuilder.index(pointer, self.astbuilder.constant(i))
                    for i in range(ndim)]

    def _debug_function_call(self, b, node):
        stats = [
            b.print_(b.constant("Calling function %s (%s specializer)" % (
                                       node.name, self.specialization_name))),
            b.print_(b.constant("shape:"), *self._index_list(node.shape,
                                                             node.ndim)),
        ]
        if self.is_tiled_specializer:
            stats.append(b.print_(b.constant("blocksize:"), self.get_blocksize()))

        if not self.is_contig_specializer:
            for idx, arg in enumerate(node.arguments):
                if arg.is_array_funcarg:
                    stats.append(b.print_(b.constant("strides operand%d:" % idx),
                                          *self._index_list(arg.strides_pointer,
                                                            arg.type.ndim)))
                    stats.append(b.print_(b.constant("data pointer %d:" % idx),
                                          arg.data_pointer))

        node.body = b.stats(b.stats(*stats), node.body)

    def visit_FunctionNode(self, node):
        b = self.astbuilder

        # set this so bad people can specialize during code generation time
        node.specializer = self
        node.specialization_name = self.specialization_name
        self.function = node

        if self.context.debug:
            self._debug_function_call(b, node)

        if node.body.may_error(self.context):
            node.body = b.error_handler(node.body)

        node.body = b.stats(node.body, b.return_(node.success_value))

        self.visitchildren(node)
        return node

    def visit_ForNode(self, node):
        if node.body.may_error(self.context):
            node.body = self.astbuilder.error_handler(node.body)
        self.visitchildren(node)
        return node

    def visit_Variable(self, node):
        if node.name not in self.variables:
            self.variables[node.name] = node
        return self.visit_Node(node)

    def visit_PositionInfoNode(self, node):
        b = self.astbuidler

        posinfo = self.function.posinfo
        if posinfo:
            pos = node.posinfo
            return b.stats(
                b.assign(b.deref(posinfo.filename), b.constant(pos.filename)),
                b.assign(b.deref(posinfo.lineno), b.constant(pos.lineno)),
                b.assign(b.deref(posinfo.column), b.constant(pos.column)))

    def visit_RaiseNode(self, node):
        from minitypes import FunctionType, object_
        b = self.astbuilder

        args = [object_] * (2 + len(node.fmt_args))
        functype = FunctionType(return_type=object_, args=args)
        return b.expr_stat(
            b.funccall(b.funcname(functype, "PyErr_Format"),
                       [node.exc_var, node.msg_val] + node.fmt_args))

    def visit_ErrorHandler(self, node):
        b = self.astbuilder

        node.error_variable = b.temp(minitypes.bool_)
        node.error_var_init = b.assign(node.error_variable, 0)
        node.cleanup_jump = b.jump(node.cleanup_label)
        node.error_target_label = b.jump_target(node.error_label)
        node.cleanup_target_label = b.jump_target(node.cleanup_label)
        node.error_set = b.assign(node.error_variable, 1)

        if self.error_handlers:
            cascade_code = b.jump(self.error_handlers[-1].error_label)
        else:
            cascade_code = b.return_(self.function.error_value)

        node.cascade = b.if_(node.error_variable, cascade_code)

        self.error_handlers.append(node)
        self.visitchildren(node)
        self.error_handlers.pop()
        return node

class OrderedSpecializer(Specializer):
    def loop_order(self, order, ndim=None):
        if ndim is None:
            ndim = self.function.ndim

        if order == "C":
            return self.c_loop_order(ndim)
        else:
            return self.f_loop_order(ndim)

    def c_loop_order(self, ndim):
        return ndim - 1, -1, -1

    def f_loop_order(self, ndim):
        return 0, ndim, 1

    def order_indices(self, indices):
        """
        Put the indices of the for loops in the right iteration order.
        """
        if self.order == "C":
            indices.reverse()

    def ordered_loop(self, node, result_indices, lower=None, upper=None,
                     step=None, loop_order=None):
        b = self.astbuilder

        if lower is None:
            lower = lambda i: None
        if upper is None:
            upper = lambda i: b.shape_index(i, self.function)
        if loop_order is None:
            loop_order = self.loop_order(self.order)

        indices = []
        # print range(*self.loop_order(self.order))
        for i in range(*loop_order):
            node = b.for_range_upwards(node, lower=lower(i), upper=upper(i),
                                       step=step)
            indices.append(node.target)

        self.order_indices(indices)
        result_indices.extend(indices)
        return node

class StridedSpecializer(OrderedSpecializer):

    specialization_name = "strided"

    order = "C"

    def visit_NDIterate(self, node):
        b = self.astbuilder
        self.indices = []
        node = self.ordered_loop(node.body, self.indices)
        node = b.omp_for(node)
        return self.visit(node)

    def visit_Variable(self, node):
        if node.name in self.function.args and node.type.is_array:
            return self._element_location(node)

        return super(StridedSpecializer, self).visit_Variable(node)

    def _element_location(self, node, indices=None, strides_index_offset=0,
                          ndim=None):
        indices = indices or self.indices
        b = self.astbuilder
        if ndim is None:
            ndim = node.type.ndim
        indices = [b.mul(index, b.stride(node, i + strides_index_offset))
                       for i, index in enumerate(indices[len(indices) - ndim:])]
        pointer = b.cast(b.data_pointer(node),
                         minitypes.char.pointer())
        node = b.index_multiple(pointer, indices,
                                dest_pointer_type=node.type.dtype.pointer())
        self.visitchildren(node)
        return node

class StridedFortranSpecializer(StridedSpecializer):
    specialization_name = "strided_fortran"
    order = "F"

class ContigSpecializer(StridedSpecializer):

    specialization_name = "contig"
    is_contig_specializer = True

    def visit_FunctionNode(self, node):
        b = self.astbuilder

        # compute the product of the shape and insert it into the function body
        extents = [b.index(node.shape, b.constant(i))
                       for i in range(node.ndim)]
        node.total_shape = b.temp(node.shape.type.base_type.unqualify("const"))
        init_shape = b.assign(node.total_shape, reduce(b.mul, extents))
        node.body = b.stats(init_shape, node.body)

        return super(ContigSpecializer, self).visit_FunctionNode(node)

    def visit_NDIterate(self, node):
        b = self.astbuilder
        node = b.omp_for(b.for_range_upwards(
                    node.body, upper=self.function.total_shape))
        self.target = node.for_node.target
        return self.visit(node)

    def visit_StridePointer(self, node):
        return None

    def _element_location(self, node):
        data_pointer = self.astbuilder.data_pointer(node)
        return self.astbuilder.index(data_pointer, self.target)

class CTiledStridedSpecializer(StridedSpecializer):

    specialization_name = "tiled_c"
    order = "C"
    is_tiled_specializer = True

    def get_blocksize(self):
        return self.astbuilder.constant(128)

    def tiled_order(self):
        "Tile in the last two dimensions"
        return self.function.ndim - 1, self.function.ndim - 1 - 2, -1

    def untiled_order(self):
        return self.function.ndim - 1 - 2, -1, -1

    def visit_NDIterate(self, node):
        return self._tile_in_two_dimensions(node)

    def _tile_in_two_dimensions(self, node):
        """
        This version generates tiling loops in the first or last two dimensions
        (depending on C or Fortran order).
        """
        b = self.astbuilder

        self.tiled_indices = []
        self.indices = []
        self.blocksize = self.get_blocksize()

        tiled_loop_body = b.stats(b.constant(0)) # fake empty loop body
        body = self.ordered_loop(tiled_loop_body, self.tiled_indices,
                                 step=self.blocksize,
                                 loop_order=self.tiled_order())
        body = b.omp_for(body)
        del tiled_loop_body.stats[:]

        upper_limits = {}
        stats = []
        tiled_order = range(*self.tiled_order())
        for i, index in zip(tiled_order, self.tiled_indices):
            upper_limit = b.temp(index.type)
            tiled_loop_body.stats.append(
                b.assign(upper_limit, b.min(b.add(index, self.blocksize),
                                            b.shape_index(i, self.function))))
            upper_limits[i] = upper_limit

        tiled_indices = dict(zip(tiled_order, self.tiled_indices))
        def lower(i):
            if i in tiled_indices:
                return tiled_indices[i]
            return None

        def upper(i):
            if i in upper_limits:
                return upper_limits[i]
            return b.shape_index(i, self.function)

        outer_for_node = node.body
        inner_body = node.body
        tiled_loop_body.stats.append(self.ordered_loop(
                node.body, self.indices,
                lower=lower, upper=upper,
                loop_order=self.tiled_order()))

        indices = []
        body = self.ordered_loop(body, indices,
                                 loop_order=self.untiled_order())

        # At this point, 'self.indices' are the indices of the tiled loop
        # (the indices in the first two dimensions for Fortran,
        #  the indices in the last two # dimensions for C)
        # 'indices' are the indices of the outer loops
        if self.order == "C":
            self.indices = indices + self.indices
        else:
            self.indices = self.indices + indices

        return self.visit(body)

    def _tile_in_all_dimensions(self, node):
        """
        This version generates tiling loops in all dimensions.
        """
        b = self.astbuilder

        self.tiled_indices = []
        self.indices = []
        self.blocksize = self.get_blocksize()

        tiled_loop_body = b.stats(b.constant(0)) # fake empty loop body
        body = self.ordered_loop(tiled_loop_body, self.tiled_indices,
                                 step=self.blocksize)
        body = b.omp_for(body)
        del tiled_loop_body.stats[:]

        upper_limits = []
        stats = []
        for i, index in enumerate(self.tiled_indices):
            upper_limit = b.temp(index.type)
            tiled_loop_body.stats.append(
                b.assign(upper_limit, b.min(b.add(index, self.blocksize),
                                            b.shape_index(i, self.function))))
            upper_limits.append(upper_limit)

        tiled_loop_body.stats.append(self.ordered_loop(
            node.body, self.indices,
            lower=lambda i: self.tiled_indices[i],
            upper=lambda i: upper_limits[i]))
        return self.visit(body)

class FTiledStridedSpecializer(CTiledStridedSpecializer):

    specialization_name = "tiled_fortran"
    order = "F"

    def tiled_order(self):
        "Tile in the first two dimensions"
        return 0, 2, 1

    def untiled_order(self):
        return 2, self.function.ndim, 1


class StridedCInnerContigSpecializer(StridedSpecializer):

    specialization_name = "inner_contig_c"
    order = "C"

    def visit_NDIterate(self, node):
        b = self.astbuilder
        self.indices = []
        node = self.ordered_loop(node.body, self.indices)

        loop = node
        for index in self.indices[:-2]:
            loop = node.body

        self.pointers = {}
        stats = []
        for arg in self.function.arguments:
            if arg.is_array_funcarg:
                dest_pointer_type = arg.data_pointer.type.unqualify('const')
                pointer = b.temp(dest_pointer_type)

                sup = super(StridedCInnerContigSpecializer, self)
                first_element_pointer = sup._element_location(
                                    arg, indices=self.strided_indices(),
                                    strides_index_offset=self.order == 'F',
                                    ndim=arg.type.ndim - 1)
                stats.append(b.assign(pointer, first_element_pointer.operand))
                self.pointers[arg.variable] = pointer

        loop.body = b.stats(*(stats + [loop.body]))
        return self.visit(b.omp_for(node))

    def strided_indices(self):
        return self.indices[:-1]

    def contig_index(self):
        return self.indices[-1]

    def _element_location(self, variable):
        data_pointer = self.pointers[variable]
        return self.astbuilder.index(data_pointer, self.contig_index())

class StridedFortranInnerContigSpecializer(StridedCInnerContigSpecializer):

    order = "F"
    specialization_name = "inner_contig_fortran"

    def strided_indices(self):
        return self.indices[1:]

    def contig_index(self):
        return self.indices[0]