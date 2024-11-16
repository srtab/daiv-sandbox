# daiv-sandbox

`daiv-sandbox` is a FastAPI application designed to securely execute arbitrary commands and untrusted code within a controlled environment. Each execution is isolated in a transient Docker container, which is automatically created and destroyed with every request, ensuring a clean and secure execution space.

To enhance security, `daiv-sandbox` leverages [`gVisor`](https://github.com/google/gvisor) as its container runtime. This provides an additional layer of protection by restricting the running code's ability to interact with the host system, thereby minimizing the risk of sandbox escape.

While `gVisor` significantly improves security, it may introduce some performance overhead due to its additional isolation mechanisms. This trade-off is generally acceptable for applications prioritizing security over raw execution speed.
