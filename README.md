# Synology NAS MCP

[![CI](https://github.com/miku233333/synology-nas-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/miku233333/synology-nas-mcp/actions/workflows/ci.yml)

Self-hosted MCP access to Synology NAS files, monitoring, containers and downloads.

Synology NAS 的自架 MCP 服務。在 Container Manager 執行，透過 MCP 客戶端讀取文件、查詢 NAS 狀態，並按需啟用容器及下載操作。支援 Streamable HTTP、stdio，以及可選的 OpenAI Secure MCP Tunnel。

**Alpha：MCP 連線、檔案讀取與容器部署已於 DS720+／DSM 7.3 實機驗證；DSM API 工具、公開 OAuth 接入與 ChatGPT 連通仍待驗證。** 本專案為社群專案，與 Synology、OpenAI 沒有隸屬關係。

## 功能

| 工具 | 用途 | 啟用條件 |
| --- | --- | --- |
| `list_files` | 列出資料夾、分頁 | 唯讀資料夾掛載 |
| `search_files` | 遞迴比對檔名，不分大小寫 | 唯讀資料夾掛載 |
| `read_file` | 讀取 UTF-8、PDF、DOCX 文字 | 唯讀資料夾掛載 |
| `get_system_info` | NAS 型號、版本、溫度等 | DSM 帳戶 |
| `get_resource_usage` | CPU、記憶體、網路、磁碟 I/O | DSM 帳戶 |
| `get_storage_info` | 磁碟及儲存空間狀態 | DSM 帳戶 |
| `list_containers` | 容器狀態摘要 | DSM 帳戶及 Container Manager |
| `list_download_tasks` | 下載任務摘要 | DSM 帳戶及 Download Station |
| `control_container` | 啟動、停止、重啟指定容器 | 操作開關及精確名稱清單 |
| `create_download_task` | 在固定目的地建立下載 | 操作開關；受限 magnet URI |
| `control_download_task` | 暫停、繼續目的地範圍內的任務 | 操作開關及固定目的地 |

PDF 支援文字層；DOCX 擷取主文件段落。沒有 OCR、全文索引或排版還原。檔案名稱和內容以原文回傳。容器工具不回傳環境變數；下載摘要不回傳來源 URL 或帳密。

## 安裝到 Synology

需要支援 Container Manager／Docker Compose 的 NAS。此專案使用 Linux 的安全檔案開啟介面；本機開發亦支援 macOS。

1. 下載或 clone 本倉庫，放到 NAS 的專案資料夾：

   ```sh
   git clone https://github.com/miku233333/synology-nas-mcp.git
   cd synology-nas-mcp
   cp .env.example .env
   chmod 600 .env
   ```

2. 建立專用共享資料夾，例如 `/volume1/AI-Share`，放入可讓 MCP 客戶端讀取的文件。在 `.env` 設定：

   - `NAS_SHARE_PATH`：現有資料夾的絕對路徑。
   - `NAS_UID`、`NAS_GID`：有權讀取該資料夾的 DSM 使用者數字 UID/GID，可用 `id 使用者名稱` 查詢。不要用 root。
   - `MCP_AUTH_TOKEN`：自行執行 `openssl rand -hex 32` 產生，填入結果。

   Token、密碼及 `.env` 留在自己的 NAS；不要加入 Git。含 `$` 或 `#` 的 `.env` 密碼請使用單引號包住。

3. 啟動：

   ```sh
   docker compose up -d --build
   docker compose ps
   ```

   亦可在 DSM **Container Manager → 專案** 匯入此資料夾及 `compose.yaml`。首版由原始碼建置，未提供預先發佈的專案 image。

預設只掛載選定資料夾，容器內路徑是 `/data`，讀取請用 `報告.pdf` 等相對路徑。掛載及容器檔案系統均為唯讀。預設不發佈主機連接埠；服務只供同一 Docker 網路內的客戶端或 Tunnel 連線。

### 接到 ChatGPT 網頁版

OpenAI Secure MCP Tunnel 是可選的傳輸元件；MCP 本身不需要 OpenAI API key，也不依賴任何專案維護者的伺服器。

1. 在 [OpenAI Platform Tunnels](https://platform.openai.com/settings/organization/tunnels) 建立 Tunnel，取得 `tunnel_id`。執行 Tunnel 的 API key 需要 Tunnels **Read + Use**；建立 Tunnel 另需 **Manage**。
2. 把 Tunnel 關聯到要使用的 ChatGPT workspace。在 `.env` 填入自己的 `CONTROL_PLANE_API_KEY`、`CONTROL_PLANE_TUNNEL_ID`。這裡使用 OpenAI Platform 的 runtime API key，與 `MCP_AUTH_TOKEN` 不同。
3. 啟動可選 profile：

   ```sh
   docker compose --profile chatgpt up -d --build
   docker compose --profile chatgpt ps
   ```

4. 在 ChatGPT 啟用 Developer mode，前往 [Plugins](https://chatgpt.com/plugins) 建立連接，選 **Connection → Tunnel**，選取該 Tunnel。本部署沒有使用者層 OAuth；如出現認證選項，選 **No Authentication**。Tunnel 與 NAS MCP 之間仍使用設定好的 Bearer token。
5. 在新對話選取此 MCP，先測試：「列出 NAS 分享資料夾」，再讀取一份已知測試文件，比對回傳內容。管理操作另外驗證。

Tunnel 由 NAS 主動向 OpenAI 連線；NAS 須能連到 `api.openai.com:443`。不需要將 DSM 或 MCP 公開到網際網路。Mac 關機不影響 NAS 上的服務。

Tunnel 的 OpenAI API 控制面亦受[支援國家及地區](https://help.openai.com/en/articles/5347006-openai-api-supported-countries-and-territories)限制。若容器日誌出現 `403 unsupported_country_region_territory`，代表目前部署出口不受支援；應停止 Tunnel 並確認部署資格，不能將容器啟動成功視為已連通。

ChatGPT 功能取決於帳戶、Developer mode 與 workspace 權限。官方開發者文件和 Help Center 對個人方案的寫入支援描述不完全一致，請以實際帳戶驗證，不把建立連接視為所有操作已可用。

參考：[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)、[Developer mode](https://developers.openai.com/api/docs/guides/developer-mode)、[Help Center](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt)。

### 公開 HTTPS 與 OAuth

需要公網接入時，先完成使用者認證，再開放 HTTPS 入口。MCP 仍只接觸掛載的指定資料夾；DSM 操作仍由獨立開關控制。

#### Cloudflare Tunnel 與 Access

已有 Cloudflare 網域的部署者可用 [Tunnel](https://developers.cloudflare.com/tunnel/get-started/) 提供 HTTPS，並以 [Access Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/) 處理 ChatGPT 登入：

1. 建立專用 Tunnel 和主機名稱，將 origin 指向同一 Compose 網路內的 `http://nas-mcp:8000`。建立僅允許自己登入的 Access 應用，啟用 Managed OAuth 及受限的動態客戶端註冊。Cloudflare 的 OAuth 探索與 401 認證要求由邊緣處理。
2. 在 NAS 的 `.env` 填入 Access team domain、此應用的 AUD tag，以及允許登入的電郵。Tunnel token 存成 NAS 上權限 `600` 的獨立檔案，不加入 Git：

   ```dotenv
   MCP_AUTH_MODE=cloudflare-access
   MCP_CF_ACCESS_ISSUER_URL=https://team.cloudflareaccess.com
   MCP_CF_ACCESS_AUDIENCE=access-app-aud-tag
   MCP_CF_ACCESS_ALLOWED_EMAILS=owner@example.com
   CLOUDFLARE_TUNNEL_TOKEN_FILE=/volume1/docker/synology-nas-mcp-secrets/cloudflare-token
   ```

3. 執行 `docker compose -f compose.yaml -f compose.cloudflare.yaml --profile cloudflare up -d --build`。先檢查未登入請求由 Access 回覆 OAuth challenge；登入後確認只可讀取預期的測試文件。NAS 會再次驗證 Access 傳到 origin 的身分 JWT；不要把容器主機埠另外公開。

#### 一般 OAuth 身分供應商

也可將 MCP 切換到 OAuth 資源伺服器模式，由自己的身分供應商處理登入。以下以 Auth0 為例，其他供應商須符合 [ChatGPT 的 MCP OAuth 要求](https://developers.openai.com/plugins/build/auth)。

1. 為此服務準備固定 HTTPS 網址，例如 `https://mcp.example.com/mcp`。反向代理或 Cloudflare Tunnel 只轉發到容器的 `nas-mcp:8000`，不公開 DSM 管理介面。若代理保留原始 `Host`，把 `mcp.example.com:*` 加入 `MCP_ALLOWED_HOSTS`。
2. 在 Auth0 建立 API，其 Identifier 與 MCP 網址完全一致，並建立 `nas.read` permission。啟用 **Resource Parameter Compatibility Profile**，讓 ChatGPT 傳入的 `resource` 成為 access token 的 `aud`。建立授權碼與 PKCE S256 客戶端，僅允許自己登入；ChatGPT 的完整回跳網址須以連接設定頁顯示者為準。
3. 在 NAS 的 `.env` 填入以下設定。`MCP_OAUTH_ALLOWED_SUBJECTS` 使用身分供應商核發的不可變 `sub`，不要填電郵。issuer 的尾斜線須與 token `iss` 完全一致。

   ```dotenv
   MCP_AUTH_MODE=oauth
   MCP_OAUTH_ISSUER_URL=https://tenant.example/
   MCP_OAUTH_JWKS_URL=https://tenant.example/.well-known/jwks.json
   MCP_OAUTH_RESOURCE_URL=https://mcp.example.com/mcp
   MCP_OAUTH_ALLOWED_SUBJECTS=provider|owner-id
   MCP_OAUTH_REQUIRED_SCOPE=nas.read
   MCP_ALLOWED_HOSTS=localhost:*,127.0.0.1:*,nas-mcp:*,mcp.example.com:*
   ```

4. 重建 `nas-mcp` 後，先測試 OAuth 探索文件可公開取得、未帶 token 的 `/mcp` 回覆 401，且錯誤受眾、過期或其他帳戶的 token 都無法讀檔。在 ChatGPT 網頁版開啟 Developer mode，於 [Plugins](https://chatgpt.com/plugins) 加入 HTTPS MCP 網址，選 OAuth，填入身分供應商的 client ID／secret，完成登入後再測試一份已知文件。

私有 Bearer 模式與 OpenAI Tunnel 仍可獨立使用。切換成公開認證模式前，不要直接把原有 Bearer 入口發佈到公網。公開網址與 OAuth 不會改變 ChatGPT 使用者的[地區資格](https://help.openai.com/en/articles/7947663-chatgpt-supported-countries)。

### 其他 MCP 客戶端／本機開發

stdio 不需要 HTTP token：

```sh
uv sync --locked
MCP_TRANSPORT=stdio NAS_DATA_ROOT=/path/to/shared-folder uv run synology-nas-mcp
```

HTTP 客戶端在同一 Docker 網路連到 `http://nas-mcp:8000/mcp`，加上 `Authorization: Bearer <MCP_AUTH_TOKEN>`。如需從 NAS 主機本地測試，可建立 `compose.local.yaml`：

```yaml
services:
  nas-mcp:
    ports:
      - "127.0.0.1:8000:8000"
```

```sh
docker compose -f compose.yaml -f compose.local.yaml up -d
```

此 HTTP token 是私有部署的服務認證；公開 HTTPS 入口請使用上述 OAuth 模式、TLS 及存取控制。

## DSM 管理工具

只使用檔案功能時，留空所有 `DSM_*` 即可。要查詢 DSM，填入完整的三個設定：

```dotenv
DSM_URL=https://nas.example.net:5001
DSM_USERNAME=mcp-service
DSM_PASSWORD='your-local-password'
```

建立專用 DSM 帳戶，僅授予所需套件及資料權限。套件或 DSM API 若要求帳戶沒有的權限，工具會回報錯誤；不同 DSM 版本的系統／Container Manager API 可能需要管理員權限，本專案不會提升帳戶權限。不要以主帳戶作為服務帳戶。

目前不支援互動式 2FA 登入。啟用 2FA 的帳戶會無法登入；不要為此停用日常管理員帳戶的 2FA。部署者須自行選擇符合其安全政策的服務帳戶安排。

`DSM_URL` 必須使用 HTTPS 並通過憑證驗證。私人 CA 可透過唯讀掛載 CA 檔案，再設定 `DSM_CA_BUNDLE=/certs/ca.pem`；此路徑必須是容器內的路徑。

### 啟用指定容器操作

```dotenv
NAS_ENABLE_CONTAINER_ACTIONS=true
NAS_ALLOWED_CONTAINERS=demo-web,media-indexer
```

只接受清單中完全相同的名稱及 `start`、`stop`、`restart`。讀取檔案仍維持唯讀。不要將 Tunnel、MCP 本身或關鍵網路容器放入操作清單。

### 啟用下載操作

```dotenv
NAS_ENABLE_DOWNLOAD_ACTIONS=true
NAS_DOWNLOAD_DESTINATION=Downloads/MCP
```

目的地使用 Download Station 的 `共享資料夾/子資料夾` 格式，必須存在且服務帳戶有權寫入。對話不能更改目的地；暫停／繼續前會核對任務目的地。

首版建立下載只接受具有有效 BTIH hash 的 magnet URI，可附 `dn` 名稱。其他參數（包括 tracker、web seed URL）及 HTTP／HTTPS 下載不支援。操作工具回覆 `accepted` 只表示 DSM 接受請求，容器或下載結果須再查實際狀態。

## 設定與限制

| 設定 | 預設值 | 說明 |
| --- | --- | --- |
| `NAS_MAX_FILE_BYTES` | `2097152` | 單份檔案最多 2 MiB |
| `NAS_MAX_TEXT_CHARS` | `50000` | 回傳文字字數上限 |
| `NAS_MAX_SEARCH_ENTRIES` | `10000` | 每次搜尋／列目錄的掃描上限 |
| `MCP_TRANSPORT` | `streamable-http` | 本機亦可用 `stdio` |
| `MCP_AUTH_TOKEN` | 必填 | HTTP 服務 token，至少 32 個可見 ASCII 字元 |
| `MCP_ALLOWED_HOSTS` | localhost／Docker 服務名 | 直接執行時的允許 Host 清單 |

完整部署參數見 [.env.example](.env.example)。直接執行服務時，`MCP_AUTH_TOKEN_FILE`、`DSM_PASSWORD_FILE` 可從 secret 檔案讀取；使用 Compose secrets 需在自己的 Compose override 掛載檔案並配置這些變數。

- 禁止絕對路徑、父目錄越界、符號連結、FIFO 及裝置檔案。
- PDF 最多解析 200 頁，頁面與資源內容累計解碼上限 4 MiB；含大型圖片的 PDF 也可能超出此限制。DOCX 最多 1,024 個 ZIP entries、總展開量 8 MiB、主文件 XML 4 MiB。每個程序一次解析一份文件。
- 路徑最多 64 層；搜尋遇到更深子目錄會略過並回報 `truncated`。
- 回覆包含 `truncated` 時，表示結果不完整；縮小資料夾或查詢範圍。
- Compose 限制 MCP 容器記憶體 256 MiB、CPU 1 核；高壓縮或複雜文件仍可能觸及容器限額。
- 每個容器日誌最多約 30 MiB；唯讀檔案不建立額外副本、索引或備份。
- DSM 寫入失敗或逾時不會自動重播；應先查實際狀態，再由使用者決定重試。
- 工具的確認提示取決於 MCP 客戶端；伺服器上的操作開關及範圍檢查才是強制權限界線。

檔案與工具回覆會交給選用的 AI 服務。請只掛載準備分享的資料夾。詳見 [SECURITY.md](SECURITY.md)。

## 開發與驗證

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
docker build -t synology-nas-mcp:local .
```

測試涵蓋檔案越界／符號連結、格式擷取及大小限制、HTTP 認證與 MCP 呼叫、DSM session、操作權限和輸出過濾。DSM 使用模擬 HTTP 回覆；測試通過不代表特定 NAS／ChatGPT 已連通。

程式使用官方 MCP Python SDK，依賴由 `uv.lock` 鎖定。升級後重建容器；回退可 checkout 前一個已驗證的 Git commit 再重建。不要清除既有可回退 image，直到新版本驗證通過。

DSM 協定參考 Synology 官方 [DSM Login Web API Guide](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Os/DSM/All/enu/DSM_Login_Web_API_Guide_enu.pdf) 與 [Download Station Web API Guide](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/DownloadStation/All/enu/Synology_Download_Station_Web_API.pdf)。

## English quick start

This is a community-maintained alpha, not an official Synology integration. Clone the repository, copy `.env.example` to `.env`, set an existing `NAS_SHARE_PATH`, a non-root UID/GID with read access, and a random `MCP_AUTH_TOKEN`. Run `docker compose up -d --build`.

Files are mounted read-only. DSM credentials are optional; container and download actions require explicit switches and scopes. The optional `chatgpt` Compose profile runs OpenAI's Secure MCP Tunnel using your own Platform key and tunnel ID. It does not require public NAS ports. HTTP uses a private Bearer token by default; opt-in OAuth resource-server mode supports a public HTTPS endpoint with your own identity provider. stdio also works.

Reads support UTF-8 text and text extraction from PDF/DOCX, with bounded output and no OCR. Search matches filenames. Download creation accepts only restricted BTIH magnet links. NAS compatibility and ChatGPT account access require live validation. Run the commands above for local tests.

## License

[MIT](LICENSE)
