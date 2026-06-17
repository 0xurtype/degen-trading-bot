# GMGN-scanner
Run with Docker

bash
docker compose up -d --build
# → http://localhost
docker compose up -d --build
# → http://localhost

Run with plain static hosting

Just serve the public/ directory with any HTTP server:


bash
npx serve public -l 3000
# or
python3 -m http.server 3000 --directory public
npx serve public -l 3000
# or
python3 -m http.server 3000 --directory public

Deploy to VPS (Ubuntu + Docker)

bash
# 1. Install Docker
curl -fsSL https://get.docker.com | sh
# 1. Install Docker
curl -fsSL https://get.docker.com | sh
2. Clone
git clone && cd gmgn-scanner

3. Run
docker compose up -d --build

App is now live on port 80
Point your domain's A record to the VPS IP
text

## Deploy to VPS (Ubuntu + Nginx, no Docker)

```bash
sudo apt install nginx -y
sudo cp -r public/* /var/www/html/
sudo systemctl restart nginx

## Deploy to VPS (Ubuntu + Nginx, no Docker)

```bash
sudo apt install nginx -y
sudo cp -r public/* /var/www/html/
sudo systemctl restart nginx

Project structure

text
public/index.html    ← Single-file app (HTML + CSS + JS)
server.js            ← Lightweight Node dev server
Dockerfile           ← Production container
nginx.conf           ← Nginx config for container
docker-compose.yml   ← One-command deploy
public/index.html    ← Single-file app (HTML + CSS + JS)
server.js            ← Lightweight Node dev server
Dockerfile           ← Production container
nginx.conf           ← Nginx config for container
docker-compose.yml   ← One-command deploy

Next steps to production

 Replace mock data with real API calls (Solana RPC, Birdeye, Jupiter, Helius)
 Add wallet authentication (Phantom wallet connect)
 Backend service for wallet tracking persistence (Node/Go + Postgres)
 WebSocket for real-time feed
 Rate limiting & caching layer
Step 3: Push to GitHub
bash
# Create the folder
mkdir gmgn-scanner && cd gmgn-scanner

# Copy your HTML file into public/
mkdir public
# paste your index.html into public/index.html

# (paste all the files above into their locations)

# Init and push
git init
git add .
git commit -m "initial: GMGN scanner prototype with wallet tracker"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/gmgn-scanner.git
git push -u origin main
# Create the folder
mkdir gmgn-scanner && cd gmgn-scanner

# Copy your HTML file into public/
mkdir public
# paste your index.html into public/index.html

# (paste all the files above into their locations)

# Init and push
git init
git add .
git commit -m "initial: GMGN scanner prototype with wallet tracker"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/gmgn-scanner.git
git push -u origin main
Step 4: Deploy on your VPS
Option A — Docker (recommended, takes 2 minutes):

bash
ssh your-user@your-vps-ip

git clone https://github.com/YOUR_USERNAME/gmgn-scanner.git
cd gmgn-scanner
docker compose up -d --build

# That's it. Visit http://your-vps-ip
ssh your-user@your-vps-ip

git clone https://github.com/YOUR_USERNAME/gmgn-scanner.git
cd gmgn-scanner
docker compose up -d --build

# That's it. Visit http://your-vps-ip
Option B — Bare Nginx (no Docker):

bash
sudo apt install nginx -y
git clone https://github.com/YOUR_USERNAME/gmgn-scanner.git
sudo cp -r gmgn-scanner/public/* /var/www/html/
sudo systemctl restart nginx
sudo apt install nginx -y
git clone https://github.com/YOUR_USERNAME/gmgn-scanner.git
sudo cp -r gmgn-scanner/public/* /var/www/html/
sudo systemctl restart nginx
Option C — Local only:

bash
cd gmgn-scanner
npm install
npm start
# → http://localhost:3000
cd gmgn-scanner
npm install
npm start
# → http://localhost:3000
Production roadmap
Once it's running, the path from prototype to real product:

1.Real data — swap mock generators for Solana RPC calls + Helius/Birdeye/Jupiter APIs
2.Wallet connect — Phantom/Solflare integration for auth
3.Backend — Node.js or Go API + PostgreSQL for wallet persistence, trade history, alerts
4.WebSocket — real-time token feed instead of manual scan button
5.Auth & billing — if you want to monetize it
The HTML file is the entire prototype — everything else is just deployment scaffolding. You can start iterating on public/index.html immediately.
