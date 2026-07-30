# The build worker is the product, so the image has to carry a real Flutter and
# Android toolchain — roughly 4 GB once Gradle has warmed up. That is why this
# targets a VM with a disk rather than a serverless runtime.
#
# Every version here is pinned to what the verification table was proven on
# (Flutter 3.44.8 / Dart 3.12.2, Android SDK 36, build-tools 36.0.0, JDK 17).
# Floating any of them turns a reproducible build environment into a moving one,
# and the whole promise of this service is that generated code compiles.
#
# Layer order is deliberate and not just about caching: everything needing root
# happens before the switch to the build user, because a `RUN` after `USER`
# cannot create a directory under `/` or write to the system site-packages, and
# both mistakes fail the build in a way that reads like a Python problem.
FROM python:3.12-slim-bookworm

# The Flutter Linux SDK is published for x64 only, and the Android build-tools
# binaries are x86_64 ELF. On an arm64 VM — which is the cheap default at most
# providers now — this would otherwise fail several minutes in, inside Gradle,
# with an exec-format error that names none of this.
RUN arch="$(dpkg --print-architecture)"; \
    if [ "$arch" != "amd64" ]; then \
        echo "This image is amd64-only: the Flutter Linux SDK and the Android" >&2; \
        echo "build-tools ship x86_64 binaries. Detected: $arch." >&2; \
        exit 1; \
    fi

# Debian bookworm carries JDK 17, which is the version the Android toolchain is
# pinned to — no third-party apt repository needed. `xz-utils` unpacks Flutter,
# `unzip` the Android command-line tools; `git` is not optional, since the
# Flutter tool reads its own version out of the checkout.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        openjdk-17-jdk-headless \
        unzip \
        xz-utils \
        zip \
    && rm -rf /var/lib/apt/lists/*

ENV FLUTTER_VERSION=3.44.8 \
    FLUTTER_ROOT=/opt/flutter \
    ANDROID_SDK_ROOT=/opt/android \
    ANDROID_HOME=/opt/android \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
ENV PATH="${FLUTTER_ROOT}/bin:${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin:${ANDROID_SDK_ROOT}/platform-tools:${PATH}"

RUN curl -fsSL --retry 3 \
        "https://storage.googleapis.com/flutter_infra_release/releases/stable/linux/flutter_linux_${FLUTTER_VERSION}-stable.tar.xz" \
        -o /tmp/flutter.tar.xz \
    && tar -xJf /tmp/flutter.tar.xz -C /opt \
    && rm /tmp/flutter.tar.xz

# The SDK is installed by the command-line tools, which expect to live at
# cmdline-tools/latest — sdkmanager fails with a version-resolution error from
# anywhere else, and the message does not say so.
#
# The two `test` lines are the real check. `flutter doctor` reports on the
# toolchain but exits 0 with pieces missing, and `apksigner` in particular is
# what src/build/signing.py shells out to in order to prove a release APK is
# unsigned — an image without it packages fine and fails at the last step.
RUN curl -fsSL --retry 3 \
        "https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip" \
        -o /tmp/cmdline-tools.zip \
    && mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools" \
    && unzip -q /tmp/cmdline-tools.zip -d "${ANDROID_SDK_ROOT}/cmdline-tools" \
    && mv "${ANDROID_SDK_ROOT}/cmdline-tools/cmdline-tools" "${ANDROID_SDK_ROOT}/cmdline-tools/latest" \
    && rm /tmp/cmdline-tools.zip \
    && yes | sdkmanager --licenses > /dev/null \
    && sdkmanager --install "platforms;android-36" "build-tools;36.0.0" "platform-tools" > /dev/null \
    && test -d "${ANDROID_SDK_ROOT}/platforms/android-36" \
    && test -x "${ANDROID_SDK_ROOT}/build-tools/36.0.0/apksigner"

# Python dependencies, installed system-wide **as root**. With
# POETRY_VIRTUALENVS_CREATE=false these land in /usr/local, which the build user
# can read and must not be able to write; doing this after the USER switch is a
# permission error, and doing it with `pip install --user` puts `uvicorn`
# somewhere PATH does not look.
ENV POETRY_VIRTUALENVS_CREATE=false \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./
RUN pip install "poetry>=2.0" \
    && poetry install --only main --no-root \
    && command -v uvicorn > /dev/null

# Builds run as a non-root user: the Flutter tool refuses some operations as
# root, and git reports "dubious ownership" on a checkout it does not own, which
# surfaces as a Flutter version error rather than a permissions one.
#
# /data/builds is created here, while still root. `/` is not writable by the
# build user, so a `mkdir` after the switch cannot create it.
RUN useradd --create-home --shell /bin/bash builder \
    && mkdir -p /data/builds \
    && chown -R builder:builder /opt/flutter /opt/android /data/builds /app

USER builder
RUN git config --global --add safe.directory /opt/flutter \
    && flutter config --android-sdk "${ANDROID_SDK_ROOT}" --no-analytics \
    && flutter precache --android \
    && flutter --version

COPY --chown=builder:builder src ./src

# Generated projects and their APKs. Mount a volume here: a rebuilt container
# with this on the overlay filesystem loses every artifact a buyer has paid for
# and not yet downloaded.
ENV BUILD_ROOT=/data/builds
VOLUME ["/data/builds"]

EXPOSE 8000

# An API process builds as well as accepts unless BUILD_WORKER_EMBEDDED=0, so
# this one container is the whole service. One worker per container: builds are
# CPU- and disk-heavy, and two Gradle builds in one container contend for both.
CMD ["uvicorn", "src.service.app:app", "--host", "0.0.0.0", "--port", "8000"]
