#!/usr/bin/env bash

# set -uo pipefail

# PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# cd "$PROJECT_ROOT"

# POSTGRES_DSN="postgresql://velo_claim:velo_claim_dev_password@localhost:5433/velo_claim"
# REDIS_HOST="localhost"
# REDIS_PORT=6379
# MINIO_ENDPOINT="http://localhost:9000"
# MINIO_ACCESS_KEY="velo_claim"
# MINIO_SECRET_KEY="velo_claim_dev_password"

# echo -e "\e[36mStarting Velo Claim dev environment...\e[0m"
# echo -e "\e[90mProject root: $PROJECT_ROOT\e[0m"

# echo -e "\e[33mChecking Velo Claim containers...\e[0m"
# # docker ps --format 'table {{.Names}}\t{{.Status}}' | grep velo-claim || true

# echo -e "\e[33mTesting connections...\e[0m"

# export VELO_START_POSTGRES_DSN="$POSTGRES_DSN"
# export VELO_START_REDIS_HOST="$REDIS_HOST"
# export VELO_START_REDIS_PORT="$REDIS_PORT"
# export VELO_START_MINIO_ENDPOINT="$MINIO_ENDPOINT"
# export VELO_START_MINIO_ACCESS_KEY="$MINIO_ACCESS_KEY"
# export VELO_START_MINIO_SECRET_KEY="$MINIO_SECRET_KEY"

# python3 - <<'EOF'
# import os

# import boto3
# import psycopg
# import redis
# from botocore.config import Config

# postgres_dsn = os.environ["VELO_START_POSTGRES_DSN"]
# redis_host = os.environ["VELO_START_REDIS_HOST"]
# redis_port = int(os.environ["VELO_START_REDIS_PORT"])
# minio_endpoint = os.environ["VELO_START_MINIO_ENDPOINT"]
# minio_access_key = os.environ["VELO_START_MINIO_ACCESS_KEY"]
# minio_secret_key = os.environ["VELO_START_MINIO_SECRET_KEY"]

# try:
#     conn = psycopg.connect(postgres_dsn, connect_timeout=10)
#     conn.close()
#     print("  Postgres  OK")
# except Exception as e:
#     print(f"  Postgres  FAIL: {e}")

# try:
#     r = redis.Redis(host=redis_host, port=redis_port, db=0, socket_connect_timeout=10, socket_timeout=10)
#     r.ping()
#     print("  Redis     OK")
# except Exception as e:
#     print(f"  Redis     FAIL: {e}")

# try:
#     s3 = boto3.client(
#         "s3",
#         endpoint_url=minio_endpoint,
#         aws_access_key_id=minio_access_key,
#         aws_secret_access_key=minio_secret_key,
#         config=Config(connect_timeout=10, read_timeout=10, retries={"max_attempts": 0}),
#     )
#     buckets = [bucket["Name"] for bucket in s3.list_buckets()["Buckets"]]
#     print(f"  MinIO     OK: {buckets}")
# except Exception as e:
#     print(f"  MinIO     FAIL: {e}")

# print("")
# print("All systems ready. Happy coding!")
# EOF

# echo ""
# echo -e "\e[36mNext steps:\e[0m"
# echo "  1. Start the backend: npm run api"
# echo "  2. Start the frontend: npm run dev"
# echo "  3. Open frontend: http://127.0.0.1:5173 (or the DGX's LAN IP if browsing remotely)"
# echo "  4. API health: http://127.0.0.1:8002/health"




set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# Changed from localhost:5433 to the service name 'postgres' on standard port 5432
POSTGRES_DSN="postgresql://velo_claim:velo_claim_dev_password@postgres:5432/velo_claim"

# Changed from localhost to the service name 'redis'
REDIS_HOST="redis"
REDIS_PORT=6379

# Changed from localhost:9002 to the service name 'minio' on standard port 9000
MINIO_ENDPOINT="http://minio:9000"
MINIO_ACCESS_KEY="velo_claim"
MINIO_SECRET_KEY="velo_claim_dev_password"

NEO4J_URI="bolt://neo4j:7687"
NEO4J_USER="neo4j"
NEO4J_PASSWORD="password"

echo -e "\e[36mStarting Velo Claim dev environment...\e[0m"
echo -e "\e[90mProject root: $PROJECT_ROOT\e[0m"

echo -e "\e[33mChecking Velo Claim containers...\e[0m"
# docker ps --format 'table {{.Names}}\t{{.Status}}' | grep velo-claim || true

echo -e "\e[33mTesting connections...\e[0m"

export VELO_START_POSTGRES_DSN="$POSTGRES_DSN"
export VELO_START_REDIS_HOST="$REDIS_HOST"
export VELO_START_REDIS_PORT="$REDIS_PORT"
export VELO_START_MINIO_ENDPOINT="$MINIO_ENDPOINT"
export VELO_START_MINIO_ACCESS_KEY="$MINIO_ACCESS_KEY"
export VELO_START_MINIO_SECRET_KEY="$MINIO_SECRET_KEY"
export VELO_START_NEO4J_URI="$NEO4J_URI"
export VELO_START_NEO4J_USER="$NEO4J_USER"
export VELO_START_NEO4J_PASSWORD="$NEO4J_PASSWORD"



python3 - <<'EOF'
import os

import boto3
import psycopg
import redis
import time
from botocore.config import Config

postgres_dsn = os.environ["VELO_START_POSTGRES_DSN"]
redis_host = os.environ["VELO_START_REDIS_HOST"]
redis_port = int(os.environ["VELO_START_REDIS_PORT"])
minio_endpoint = os.environ["VELO_START_MINIO_ENDPOINT"]
minio_access_key = os.environ["VELO_START_MINIO_ACCESS_KEY"]
minio_secret_key = os.environ["VELO_START_MINIO_SECRET_KEY"]
neo4j_uri = os.environ["VELO_START_NEO4J_URI"]
neo4j_user = os.environ["VELO_START_NEO4J_USER"]
neo4j_password = os.environ["VELO_START_NEO4J_PASSWORD"]

for i in range(15):
    try:
        conn = psycopg.connect(postgres_dsn, connect_timeout=10)
        conn.close()
        print("  Postgres  OK")
        break
    except Exception as e:
        if i == 14:
            print(f"  Postgres  FAIL: {e}")
        else:
            time.sleep(1)

try:
    r = redis.Redis(host=redis_host, port=redis_port, db=0, socket_connect_timeout=10, socket_timeout=10)
    r.ping()
    print("  Redis     OK")
except Exception as e:
    print(f"  Redis     FAIL: {e}")

try:
    from neo4j import GraphDatabase

    for i in range(10):
        try:
            driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
            driver.verify_connectivity()
            driver.close()
            print("  Neo4j     OK")
            break
        except Exception as e:
            if i == 9:
                print(f"  Neo4j     FAIL: {e}")
            else:
                time.sleep(1)
except ImportError as e:
    print(f"  Neo4j     FAIL: neo4j driver not installed ({e})")

try:
	for i in range(10):
		s3 = boto3.client(
			"s3",
			endpoint_url=minio_endpoint,
			aws_access_key_id=minio_access_key,
			aws_secret_access_key=minio_secret_key,
			config=Config(connect_timeout=10, read_timeout=10, retries={"max_attempts": 0}),
		)
		buckets = [bucket["Name"] for bucket in s3.list_buckets()["Buckets"]]
		print(f"  MinIO     OK: {buckets}")
		break
except Exception as e:
        if i == 9:
            print(f"  MinIO     FAIL: {e}")
        else:
            time.sleep(1)

print("")
print("All systems ready. Happy coding!")
EOF

echo ""
echo -e "\e[36mNext steps:\e[0m"
echo "  1. Start the backend: npm run api"
echo "  2. Start the frontend: npm run dev"
echo "  3. Open frontend: http://127.0.0.1:5174 (or the DGX's LAN IP if browsing remotely)"
echo "  4. API health: http://127.0.0.1:8002/health"