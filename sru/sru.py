import cupy
from chainer import link, initializers, variable, cuda, Function
from chainer.utils import conv_nd, type_check
from cupy.cuda import function, Stream
from pynvrtc.compiler import Program
from collections import namedtuple
# from pynvrtc.interface import NVRTCInterface, NVRTCException

CUDA_SRU_KERNEL = """
extern "C" 
{
	__forceinline__ __device__ 
	float sigmoidf(float x)
	{
		return 1.f / (1.f + expf(-x));
	}

	__global__ 
	void forward(const float* __restrict__ x_ptr, const float* __restrict__ u_ptr, const float* __restrict__ bias_ptr, 
		float* __restrict__ context_ptr, float* __restrict__ hidden_state_ptr, 
		const int batchsize, const int feature_dimension, const int sequence_length, const int use_tanh)
	{
		int column = blockIdx.x * blockDim.x + threadIdx.x;
		int total_columns = batchsize * feature_dimension;
		if(column >= total_columns) return;

		const float bf = *(bias_ptr + column % feature_dimension);
		const float br = *(bias_ptr + column % feature_dimension + feature_dimension);
		const float* w_ptr = u_ptr + column * 3;
		float* ct_ptr = context_ptr + column;
		float ct = *(ct_ptr);
		float* ht_ptr = hidden_state_ptr + column;
		const float* xt_ptr = x_ptr + column;

        for(int t = 0;t < sequence_length;t++)
        {
	        float zt = *(w_ptr);
            float ft = sigmoidf((*(w_ptr + 1)) + bf);
            float rt = sigmoidf((*(w_ptr + 2)) + br);
            ct = ft * (ct - zt) + zt;
            *ct_ptr = ct;
            float g_ct = use_tanh ? tanh(ct) : ct;
            float xt = *xt_ptr;
            *ht_ptr = rt * (g_ct - xt) + xt;
        }
		*(hidden_state_ptr + column * sequence_length) = *(w_ptr);
	}
}
"""

# to avoid cupy.cuda.driver.CUDADriverError: CUDA_ERROR_NOT_INITIALIZED: initialization error
cupy.random.uniform()

prog = Program(CUDA_SRU_KERNEL.encode("utf-8"), "sru.cu".encode("utf-8"))
cuda_module = function.Module()
cuda_module.load(bytes(prog.compile().encode("utf-8")))
cuda_forward_func = cuda_module.get_function("forward")
cuda_stream = Stream()

# interface = NVRTCInterface()
# prog = interface.nvrtcCreateProgram(CUDA_SRU_KERNEL.encode("utf-8"), "sru.cu".encode("utf-8"), [], []);
# interface.nvrtcCompileProgram(prog, ["-ftz=true".encode("utf-8")])
# ptx = interface.nvrtcGetPTX(prog)
# module = function.Module()
# module.load(bytes(ptx.encode()))
# cuda_forward_func = module.get_function('forward')

class SRUFunction(Function):

	def __init__(self, use_tanh):
		self.use_tanh = use_tanh

	def check_type_forward(self, in_types):
		n_in = in_types.size()
		type_check.expect(3 <= n_in, n_in <= 4)

		x_type = in_types[0]
		w_type = in_types[1]
		b_type = in_types[2]
		print("w_type")
		print(w_type.shape)
		type_check.expect(
			x_type.dtype.kind == "f",
			w_type.dtype.kind == "f",
			b_type.dtype.kind == "f",
			x_type.ndim == 3,
			w_type.ndim == 3,
			b_type.ndim == 1,
			b_type.shape[0] == w_type.shape[0] // 3,
		)

		if type_check.eval(n_in) == 4:
			ct_type = in_types[3]
			type_check.expect(
				ct_type.dtype == x_type.dtype,
				ct_type.ndim == 2,
				ct_type.shape[0] == b_type.shape[0],
			)

	def forward_cpu(self, inputs):
		raise NotImplementedError()
			
	def forward_gpu(self, inputs):
		x, U, b = inputs[:3]
		xp = cuda.get_array_module(U)
		batchsize = x.shape[0]
		sequence_length = x.shape[2]
		feature_dimension = U.shape[1] // 3
		ct = inputs[3] if len(inputs) == 4 else xp.zeros((batchsize, feature_dimension), dtype=x.dtype)
		print(sequence_length)
		print(feature_dimension)

		total_columns = feature_dimension * batchsize
		thread_per_block = min(512, total_columns)
		num_block = total_columns // thread_per_block + 1
		print(thread_per_block)
		print(num_block)

		b[0] = 1
		b[1] = 2

		H = xp.zeros((batchsize, feature_dimension, sequence_length), dtype=x.dtype)
		print(H.shape)
		cuda_forward_func(args=[
			x.data.ptr,
			U.data.ptr,
			b.data.ptr,
			ct.data.ptr,
			H.data.ptr,
			batchsize,
			feature_dimension,
			sequence_length,
			self.use_tanh
		], block=(thread_per_block, 1, 1), grid=(num_block, 1, 1))
		import numpy
		numpy.set_printoptions(suppress=True)
		print(cuda.to_cpu(H).astype(numpy.float32))
		raise Exception()
		return U,

	def backward_cpu(self, inputs, grad_outputs):
		raise NotImplementedError()

	def backward_gpu(self, inputs, grad_outputs):
		raise NotImplementedError()

def sru(x, U, b, ct=None, use_tanh=True):
	func = SRUFunction(use_tanh)
	if ct is None:
		return func(x, U, b)
	return func(x, U, b, ct)

class SRU(link.Link):
	def __init__(self, in_channels, out_channels, use_tanh=True, initialW=None):
		super().__init__()
		self.in_channels = in_channels
		self.out_channels = out_channels
		self.use_tanh = use_tanh
		self.ct = None
		ksize = conv_nd.as_tuple(1, 1)

		with self.init_scope():
			self.U = variable.Parameter(initializers._get_initializer(initialW), (out_channels * 3, in_channels) + ksize)
			self.b = variable.Parameter(initializers._get_initializer(0), out_channels * 2)

	def __call__(self, x):
		return sru(x, self.U, self.b, self.ct, self.use_tanh)
