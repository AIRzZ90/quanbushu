# Quantus Mining Control

一个自托管的 Quantus 主网挖矿控制面板：监控算力、节点同步、GPU 状态和收益估算，并可在登录后从网页启动或停止官方 `quantus-node` / `quantus-miner`。

## 功能

- 一条 `install.sh` 命令安装固定版本的官方 Quantus node/miner。
- 前端输入 24 词 Quantus 助记词，后端通过 `quantus-node key quantus --scheme wormhole --words` 的标准输入派生奖励地址。
- 根据派生出的奖励身份启动主网节点和矿工。
- 支持 CPU worker 与 CUDA GPU 同时运行。
- 助记词不会写入 Git、`.env`、日志、命令行参数、浏览器 `localStorage` 或 API 返回值。
- 默认使用本机自签名 HTTPS；助记词启动接口拒绝普通 HTTP。

## 一键部署

在目标 Linux x86_64 服务器上执行：

```bash
git clone https://github.com/AIRzZ90/quanbushu.git /root/quantus-mining-control && cd /root/quantus-mining-control && MINING_CONTROL_PASSWORD='设置一个新的面板密码' ./install.sh
```

安装脚本会生成密码哈希和 HTTPS 证书，并安装 `supervisor` 服务。部署结束时会打印前端登录地址。Vast.ai 环境会自动使用 `PUBLIC_IPADDR` 和 `VAST_TCP_PORT_<容器端口>` 计算公网地址；其他环境可以通过 `MINING_CONTROL_PUBLIC_URL` 指定完整地址。

重复运行 `install.sh` 时，不传 `MINING_CONTROL_PASSWORD` 会保留现有面板密码；显式传入该变量会生成新的密码哈希并重置面板密码。

再次部署或更新已有目录时，使用：

```bash
cd /root/quantus-mining-control && git pull --ff-only origin main && ./install.sh
```

如果已有目录还没有 GitHub 远程地址，先执行一次：

```bash
cd /root/quantus-mining-control && git remote add origin https://github.com/AIRzZ90/quanbushu.git && git fetch origin main && git checkout -B main origin/main && ./install.sh
```

部署完成后直接打开脚本输出的“前端登录地址”。证书是自签名证书，浏览器第一次会显示证书提示；确认地址和证书后再输入助记词。

如果使用域名，可以在部署时指定证书中的主机名：

```bash
MINING_CONTROL_PUBLIC_HOST='miner.example.com' \
MINING_CONTROL_PASSWORD='面板密码' \
./install.sh
```

如果公网地址不是 Vast.ai 自动映射的地址，也可以直接指定：

```bash
MINING_CONTROL_PUBLIC_URL='https://miner.example.com:10200/' \
MINING_CONTROL_PASSWORD='面板密码' \
./install.sh
```

## 使用

1. 打开 HTTPS 面板并输入访问密码。
2. 在“启动挖矿”区域输入完整的 24 个单词，以空格分隔。
3. 检查节点名、账户索引、CPU worker 和 GPU 数量。
4. 点击启动。页面只显示派生出的公开 wormhole 奖励地址，不显示 `Inner Hash`。
5. 需要停机时点击停止挖矿。

这里的“24 位密钥”必须是 **24 个单词的助记词**，不是 24 个字符，也不是私钥十六进制字符串。

## 配置

可复制 `.env.example` 为 `.env`。`install.sh` 会自动生成密码哈希和 HTTPS 证书。常用参数：

```text
MINING_CONTROL_PORT=10200
MINING_CONTROL_CPU_WORKERS=8
MINING_CONTROL_GPU_DEVICES=1
MINING_CONTROL_CUDA_GPU=1
MINING_CONTROL_GPU_BATCH_SIZE=16777216
MINING_CONTROL_HEARTBEAT_SECONDS=300
MINING_CONTROL_HEARTBEAT_GRACE_SECONDS=180
MINING_CONTROL_MINING_DIR=/root/quantus-mining
```

如果没有显式设置 `MINING_CONTROL_GPU_DEVICES`，`install.sh` 会在有 `nvidia-smi` 时自动使用检测到的 GPU 数量，最多启用 16 张；没有 NVIDIA GPU 时回退为 1。多 GPU 服务器可以在 `.env` 中明确设置数量。

## 运行与日志

在 Vast 基础镜像上，服务由 supervisor 管理：

```bash
supervisorctl status quantus-mining-control
supervisorctl restart quantus-mining-control
tail -f /root/quantus-mining/logs/miner.log
tail -f /root/quantus-mining/logs/node.log
```

控制服务每 5 分钟检查节点进程、矿工进程、矿工指标接口和节点 RPC。发现矿工异常时，会使用
`data/launch.json` 中保存的运行配置自动恢复，不需要再次输入助记词。网页点击“停止挖矿”后会关闭自动恢复，
直到下一次从网页启动；该文件只保存派生后的运行参数，不保存助记词。

控制服务默认监听 `0.0.0.0:10200`。公网端口由服务器平台映射，不能从脚本运行时新增端口。

## 密钥处理边界

前端只在内存中持有用户刚刚输入的助记词，提交后立即清空；后端不把它写入文件，也不把它放进子进程参数，而是通过 stdin 传给官方 CLI。派生后的 `Inner Hash` 只用于当前节点进程启动参数，公开地址写入 `wallet-address` 供监控查询。

不要把真实助记词提交到 Git、Issue、聊天记录或 `.env`。如果关闭 HTTPS，必须把控制服务放在可信的 HTTPS 反向代理后面，并设置 `MINING_CONTROL_REQUIRE_HTTPS=1`。

## 版本

默认安装：

- Quantus node `v1.0.1`
- Quantus miner `v4.2.0`

可在部署时覆盖：

```bash
QUANTUS_NODE_VERSION=v1.0.1 \
QUANTUS_MINER_VERSION=v4.2.0 \
MINING_CONTROL_PASSWORD='面板密码' \
./install.sh
```

版本和奖励规则变化时，应先核对 Quantus 官方发布页和挖矿文档，再更新版本。
