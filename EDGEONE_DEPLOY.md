# 鞋底识图网站部署步骤

## 目录

- `backend/`：FastAPI 后端，负责同步、计算 pHash、搜索、图片代理。
- `edgeone-pages/`：纯静态前端，上传图片并显示结果。

## 后端本地运行

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
copy .env.example .env
uvicorn app:app --host 0.0.0.0 --port 8000
```

Mac/Linux 把最后两步换成：

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --host 0.0.0.0 --port 8000
```

打开：

```text
http://127.0.0.1:8000/health
```

看到 `{"ok":true}` 就说明后端启动成功。

## 数据源配置

在后端环境变量里配置 `DATA_SOURCES_JSON`。

美足鞋材示例：

```json
[
  {
    "name": "美足鞋材",
    "kind": "html",
    "list_url": "http://www.txmeizu.com/Product7.asp?BigClassName=&SmallClassName=",
    "url_template": "http://www.txmeizu.com/Product7.asp?BigClassName=&SmallClassName=&page={page}",
    "start_page": 1,
    "max_pages": 73,
    "encoding": "gb2312"
  }
]
```

如果是小程序真实接口，配置成 JSON 源：

```json
[
  {
    "name": "某鞋厂",
    "kind": "json",
    "list_url": "https://接口地址",
    "page_param": "page",
    "start_page": 1,
    "max_pages": 100,
    "items_field": "data.list",
    "image_field": "image_url"
  }
]
```

## 数据库

SQLite 表只有三列：

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT
image_url TEXT NOT NULL UNIQUE
phash TEXT NOT NULL
```

图片不保存到服务器硬盘，只保存图片 URL 和 pHash。

## 前端配置

修改：

```text
edgeone-pages/config.js
```

把后端地址改成你的 FastAPI 地址：

```js
window.SOLE_API_BASE = "https://你的后端域名";
```

本地测试时可以先写：

```js
window.SOLE_API_BASE = "http://127.0.0.1:8000";
```

然后直接打开：

```text
edgeone-pages/index.html
```

## EdgeOne Pages

1. 上传 `edgeone-pages/` 目录。
2. 构建命令留空。
3. 输出目录填写 `edgeone-pages`。
4. 后端 `ALLOWED_ORIGINS` 可以先填 `*`。
5. 网站打开时会自动调用 `POST /sync`。
6. 上传图片时调用 `POST /search`。

## 接口

- `GET /health`：检查后端是否启动。
- `POST /sync`：同步数据源图片，爬不到就跳过。
- `POST /search`：上传图片并返回相似结果。
- `GET /proxy?url=图片URL`：直接转发第三方图片。

## 重要说明

当前版本用 pHash，适合同图、近似图、压缩图、裁剪不大的图。它还不是淘宝级“语义识图”。如果后面要做到“侧面鞋子里看鞋底结构相似”，再升级 DINOv2 / SigLIP / CLIP 视觉向量。
