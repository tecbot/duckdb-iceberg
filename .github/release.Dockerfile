ARG MANYLINUX_IMAGE=quay.io/pypa/manylinux_2_28_x86_64
FROM ${MANYLINUX_IMAGE}
ENV PATH="/opt/python/cp314-cp314/bin:${PATH}"
RUN python --version && python -m pip install cmake==4.0.2 ninja
RUN yum install -y git zip unzip autoconf automake libtool perl-IPC-Cmd perl-core pkgconfig
RUN git clone https://github.com/microsoft/vcpkg.git /vcpkg && \
    git -C /vcpkg checkout cd61e1e26a038e82d6550a3ebbe0fbbfe7da78e3 && \
    /vcpkg/bootstrap-vcpkg.sh -disableMetrics
ENV VCPKG_TOOLCHAIN_PATH=/vcpkg/scripts/buildsystems/vcpkg.cmake
ENV STATIC_LIBCPP=1
RUN git config --global --add safe.directory '*'
