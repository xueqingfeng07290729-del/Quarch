#include <cuda_runtime.h>
#include <cublas_v2.h>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <vector>

static volatile std::sig_atomic_t stop_requested = 0;
static void stop(int) { stop_requested = 1; }
static void ck(cudaError_t e, const char* operation) {
    if (e != cudaSuccess) { std::fprintf(stderr,"%s: %s\n",operation,cudaGetErrorString(e)); std::exit(1); }
}
static void cb(cublasStatus_t e, const char* operation) {
    if (e != CUBLAS_STATUS_SUCCESS) { std::fprintf(stderr,"%s: cuBLAS status %d\n",operation,int(e)); std::exit(1); }
}
static double verify(const std::vector<float>& a, const std::vector<float>& b, float* dc, int n) {
    std::vector<float> c(size_t(n)*n);
    ck(cudaMemcpy(c.data(),dc,c.size()*sizeof(float),cudaMemcpyDeviceToHost),"download verification");
    double maximum=0;
    for (int s=0;s<64;++s) {
        const int row=(s*67)%n, col=(s*113+31)%n;
        double expected=0;
        for (int k=0;k<n;++k) expected+=double(a[row+size_t(k)*n])*b[k+size_t(col)*n];
        const double difference=std::abs(double(c[row+size_t(col)*n])-expected);
        if (!std::isfinite(difference) || difference>1e-4) {
            std::fprintf(stderr,"Matrix verification failed at %d,%d: %.9g vs %.9g\n",row,col,c[row+size_t(col)*n],expected);
            std::exit(2);
        }
        maximum=std::fmax(maximum,difference);
    }
    return maximum;
}
int main(int argc,char** argv) {
    if (argc!=4) { std::fprintf(stderr,"Usage: matmul_load max_seconds ready_json result_json\n");return 2; }
    const double max_seconds=std::atof(argv[1]);
    if (!(max_seconds>0 && max_seconds<=180)) return 2;
    std::signal(SIGTERM,stop);std::signal(SIGINT,stop);
    ck(cudaSetDeviceFlags(cudaDeviceScheduleBlockingSync),"blocking synchronization");
    ck(cudaSetDevice(0),"select device");
    cudaDeviceProp props{};ck(cudaGetDeviceProperties(&props,0),"device properties");
    constexpr int n=4096;
    const size_t count=size_t(n)*n, bytes=count*sizeof(float);
    std::vector<float> a(count),b(count);
    for (int col=0;col<n;++col) for(int row=0;row<n;++row) {
        a[row+size_t(col)*n]=float((row+7*col)%17-8)*0.125f;
        b[row+size_t(col)*n]=float((3*row+col)%19-9)*0.0625f;
    }
    float *da=nullptr,*db=nullptr,*dc=nullptr;
    ck(cudaMalloc(&da,bytes),"allocate A");ck(cudaMalloc(&db,bytes),"allocate B");ck(cudaMalloc(&dc,bytes),"allocate C");
    ck(cudaMemcpy(da,a.data(),bytes,cudaMemcpyHostToDevice),"upload A");
    ck(cudaMemcpy(db,b.data(),bytes,cudaMemcpyHostToDevice),"upload B");
    cublasHandle_t handle;cb(cublasCreate(&handle),"create cuBLAS");
    cb(cublasSetMathMode(handle,CUBLAS_PEDANTIC_MATH),"set FP32 math");
    const float alpha=1,beta=0;
    auto multiply=[&]() { cb(cublasSgemm(handle,CUBLAS_OP_N,CUBLAS_OP_N,n,n,n,&alpha,da,n,db,n,&beta,dc,n),"SGEMM"); };
    multiply();ck(cudaDeviceSynchronize(),"initial multiply");
    double max_error=verify(a,b,dc,n);
    {
        std::ofstream ready(argv[2]);
        ready<<"{\"ready\":true,\"device\":\""<<props.name<<"\",\"matrix_size\":"<<n<<",\"dtype\":\"FP32\",\"math_mode\":\"CUBLAS_PEDANTIC_MATH\",\"verified_entries\":64,\"max_abs_error\":"<<max_error<<"}\n";
        if (!ready) return 3;
    }
    const auto begin=std::chrono::steady_clock::now();
    long long completed=0;double elapsed=0;
    while (!stop_requested && elapsed<max_seconds) {
        for (int batch=0;batch<16;++batch) multiply();
        ck(cudaDeviceSynchronize(),"batch completion");completed+=16;
        elapsed=std::chrono::duration<double>(std::chrono::steady_clock::now()-begin).count();
    }
    max_error=std::fmax(max_error,verify(a,b,dc,n));
    cb(cublasDestroy(handle),"destroy cuBLAS");
    ck(cudaFree(da),"free A");ck(cudaFree(db),"free B");ck(cudaFree(dc),"free C");
    std::ofstream result(argv[3]);
    result<<"{\"passed\":true,\"device\":\""<<props.name<<"\",\"matrix_size\":"<<n<<",\"dtype\":\"FP32\",\"loop_matmuls\":"<<completed<<",\"loop_seconds\":"<<elapsed<<",\"stopped_by_signal\":"<<(stop_requested?"true":"false")<<",\"verification_entries_each\":64,\"max_abs_error\":"<<max_error<<",\"note\":\"Loop covers warmup and the measurement; this is not the matmul count within the 30-second capture.\"}\n";
    return result?0:3;
}
