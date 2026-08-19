# ODBC 드라이버 설치가 이 도구의 최대 진입장벽이다. 이미지에 담아 없앤다.
FROM python:3.12-slim

# msodbcsql18: Microsoft 저장소에서 받는다. gnupg/curl은 키 등록에만 쓰고 지운다.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] \
https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY loadgen/ ./loadgen/
COPY tools/ ./tools/
COPY comparisons.yaml ./

# 런 아티팩트·플랜·워크로드는 볼륨으로 빼는 것을 권장한다 (compose 참조).
# 컨테이너 안에만 두면 재시작 시 측정 결과가 사라진다.
RUN mkdir -p runs plans workloads

# 루트로 실행하지 않는다. 볼륨 마운트 시 호스트 쪽 권한을 맞춰야 한다면
# docker run --user "$(id -u):$(id -g)" 로 덮어쓸 수 있다.
RUN useradd -m -u 10001 loadgen && chown -R loadgen:loadgen /app
USER loadgen

EXPOSE 8010

# 0.0.0.0 바인딩은 컨테이너 안에서만 유효하다. 외부 노출은 포트 매핑으로 제어하고,
# 신뢰할 수 없는 네트워크에 열 때는 LOADGEN_PASSWORD를 반드시 설정할 것.
CMD ["uvicorn", "loadgen.app:app", "--host", "0.0.0.0", "--port", "8010"]
