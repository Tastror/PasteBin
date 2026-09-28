# Pastebin

轻量的匿名文本分享工具，界面参考 Ubuntu Pastebin。支持代码高亮、分享链接、原文查看、下载和到期清理，无需注册。

## 本地运行

需要 Python 3.10 或更高版本。在克隆后的项目目录中运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

打开 <http://127.0.0.1:8000/>。开发端口可通过 `PASTEBIN_PORT` 调整。

## 配置

站点配置通过环境变量传入，无需修改源码：

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `PASTEBIN_SITE_NAME` | 页面显示的站点名称 | `Pastebin` |
| `PASTEBIN_PUBLIC_ORIGIN` | 站点公开地址，不带路径或结尾斜杠 | 使用请求地址 |
| `PASTEBIN_DATABASE` | SQLite 文件位置 | 项目下的 `.data/pastebin.sqlite3` |
| `PASTEBIN_TRUST_PROXY` | 信任本机反向代理设置的客户端地址 | `0` |

生产环境请设置 `PASTEBIN_PUBLIC_ORIGIN`。使用附带的 Caddy 配置时设置 `PASTEBIN_TRUST_PROXY=1`。

`.env.example` 仅提供通用配置示例。实际配置应放在仓库外，并通过进程环境或 systemd 的 `EnvironmentFile` 加载；应用不会自动读取 `.env` 文件。

## 部署示例

`deploy/` 提供 systemd 服务、定时清理任务和 Caddy 配置示例，使用以下通用约定：

- 运行用户：`pastebin`
- 项目及虚拟环境：`/opt/pastebin`、`/opt/pastebin/.venv`
- 数据目录：`/var/lib/pastebin`（由 systemd 创建）
- 环境文件：`/etc/pastebin/pastebin.env`
- 应用地址：`127.0.0.1:8000`
- 示例域名：`paste.example.com`

部署时创建对应的服务用户，将项目安装到所选目录并安装依赖。把 `.env.example` 复制到仓库外的环境文件，将部署示例复制到系统配置目录，再按自己的环境调整用户、路径和域名。让 Caddy 主配置引入站点配置，并验证配置后重新加载。

安装 systemd 单元后：

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now pastebin.service pastebin-cleanup.timer
sudo systemctl status pastebin.service pastebin-cleanup.timer
sudo systemctl restart pastebin.service
sudo systemctl stop pastebin.service pastebin-cleanup.timer
```

更新代码和依赖后重启应用。实际运行配置、数据、证书和个人运维记录应保存在仓库外。

## HTTP API

```sh
curl http://127.0.0.1:8000/api/pastes \
  -H 'Content-Type: application/json' \
  -d '{"content":"Hello, world!","syntax":"text","expiry":"1d"}'
```

返回分享、原文与下载地址。`title` 和 `author` 为可选字段，`expiry` 可选 `10m`、`1h`、`1d`、`7d`。到期内容在所有读取入口均不可访问，由定时任务清理。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -v
```
