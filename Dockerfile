FROM nikolaik/python-nodejs:python3.12-nodejs20

WORKDIR /app

# 1. Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. Install Root and Frontend dependencies
COPY package.json ./
COPY frontend/package.json ./frontend/
RUN npm install && \
    cd frontend && npm install

# 3. Copy ALL source code
COPY . .

# 4. NOW set permissions safely (ignores missing folders)
RUN chmod +x /app/entrypoint.sh
RUN chmod -R 755 /app/frontend/node_modules/ || true
RUN chmod -R 755 /app/node_modules/ || true

# 5. Start the container
CMD ["/bin/bash", "/app/entrypoint.sh"]