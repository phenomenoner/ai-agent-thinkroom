# Thinkroom

[English](README.md) · **繁體中文**

**把一個高難度問題，拆成多條彼此獨立的研究路線，最後匯成一份有證據可追溯的答案。**

Thinkroom 是一套 local-first、單節點的 AI 研究服務，適合那些不能只靠模型單次回答就下結論的重要決策。它會先框定問題、分岔出彼此隔離的觀點，等所有分支完成後才開始批判，最後綜合成保留證據脈絡的研究結果。

## 為什麼需要 Thinkroom

一般 AI 回答通常只走一條路；Thinkroom 會刻意建立多條研究路線：

- **降低錨定偏誤。** 各分支在研究階段看不到彼此結果，不會互相模仿。
- **延後群體迷思。** 所有觀點先各自完成，之後才進入交叉批判。
- **讓研究過程可檢查。** 證據、來源、驗證狀態與工作狀態，都會一路連回最終綜合結論。
- **接得進既有 Agent 工作流。** 可透過 Web UI、CLI、Python SDK、REST API、MCP 工具或內附 Agent Skills 使用。
- **本機就能開始。** 預設的 deterministic `scripted` backend 不需要供應商憑證，也能驗證完整編排流程。

## 什麼時候適合用

以下問題很適合交給 Thinkroom：

- 後果重要，草率回答可能帶來明顯風險；
- 不確定性高、有爭議，或同時存在多個競爭假設；
- 需要技術、產品、營運或反方等彼此獨立的觀點；
- 需要可稽核的建議，而不是看完即丟的聊天回答。

若只是簡單查詢、確定性計算、單純改寫，或低後果且一次工具呼叫就能完成的工作，不必啟動 Thinkroom。

## 它怎麼運作

```text
FRAME → FORK → ISOLATED ROLLOUTS → EVIDENCE → DELAYED CRITIQUE → SYNTHESIZE
```

1. **框定（Frame）**問題與限制。
2. **分岔（Fork）**出彼此獨立的觀點。
3. **隔離研究（Isolated rollouts）**，避免分支過早收斂。
4. **蒐集證據（Evidence）**，保留來源與驗證狀態。
5. **延後批判（Delayed critique）**，不在各分支尚未定型時互相干擾。
6. **綜合（Synthesize）**最有支持力的結論，同時保留有意義的異議。

## 內含功能

| 介面 | 你可以做什麼 |
| --- | --- |
| Web UI | 送出問題並查看證據豐富的研究結果。 |
| CLI | 研究、查詢、列出、取消、啟動服務及執行 MCP。 |
| Python SDK | 使用遠端 `ThinkroomClient` 或嵌入式 `Thinkroom`。 |
| REST API | 以穩定的工作（job）介面整合，並支援冪等請求。 |
| MCP | `thinkroom_research`、`thinkroom_get_research`、`thinkroom_list_research` 與 `thinkroom_cancel_research`。 |
| Agent Skills | 提供安裝、觸發判斷與操作指引。 |
| Backends | 以 typed ports 隔離 deterministic `scripted`、OpenAI-compatible 與 Prime Agent adapters。 |

## 從原始碼快速開始

需求：Python 3.12+ 與 [`uv`](https://docs.astral.sh/uv/)。

```bash
uv lock --check
uv sync --locked --all-extras --dev
install -d -m 0700 .data
uv run thinkroom serve
```

另開一個終端機：

```bash
uv run thinkroom research \
  --question "Should we adopt this design?" \
  --idempotency-key demo-001
```

服務預設監聽 `127.0.0.1:8787`，並使用 deterministic `scripted` backend。

## 安裝內附 Agent Skills

Thinkroom 內含三個受管理的 Agent Skills：

| Skill | 適合載入的時機 |
| --- | --- |
| `thinkroom-trigger` | 判斷一個重要、不確定、含多個競爭假設的問題是否值得啟動 Thinkroom；瑣碎或確定性工作不要載入。 |
| `thinkroom-operate` | 透過 CLI、REST API 或 MCP 送出、輪詢、檢查、取消或解讀 Thinkroom 工作。 |
| `thinkroom-install` | 安裝、檢查或移除受管理的 Thinkroom skill 投影。 |

將它們安裝到相容的 skill root，接著驗證受管理檔案的 hash：

```bash
uv run thinkroom skills install --target ~/.hermes/skills
uv run thinkroom skills status --target ~/.hermes/skills
```

安裝器具備冪等性，而且不會覆寫未受管理或已分歧的檔案。

## 透過 MCP 連接 Hermes

先啟動服務，再註冊 Thinkroom 的 stdio MCP server：

```bash
hermes mcp add thinkroom --command "$(pwd)/.venv/bin/thinkroom" --args mcp
hermes mcp test thinkroom
```

完成設定後，請開啟新的 Hermes session 或重新載入 MCP discovery。Hermes 會以 `mcp_thinkroom_` 前綴公開這些工具。

若 `8787` 已被占用，請改用另一個 loopback port，並讓 MCP subprocess 指向相同 endpoint：

```bash
THINKROOM_PORT=18788 uv run thinkroom serve
THINKROOM_ENDPOINT=http://127.0.0.1:18788 uv run thinkroom mcp
```

## Provider backends

預設的 `scripted` backend 只能證明編排機制正常，不能證明模型研究品質。若要接 provider，可設定：

```bash
export THINKROOM_BACKEND=openai
export THINKROOM_OPENAI_API_KEY=...
```

或：

```bash
export THINKROOM_BACKEND=prime_agent
export THINKROOM_PRIME_AGENT_EXECUTABLE=...
```

完整 runtime contract 與環境變數請見 [Operations](docs/OPERATIONS.md)。

## 冪等工作

長時間執行的提交會先回傳 job handle。REST caller 使用 `Idempotency-Key`；Python SDK 使用 `ThinkroomClient.research(..., idempotency_key="demo-001")`；CLI 使用 `--idempotency-key`；MCP 則在 `thinkroom_research` 公開 `idempotency_key`。

若同一個 key 搭配不同 request 重複使用，系統會以 `IDEMPOTENCY_CONFLICT` 拒絕。

## 經 release 授權的正式安裝方式

正式安裝時，wheel、`requirements-production.txt`、`uv.lock` 與 `verify_locked_runtime.py` 必須來自**同一個 GitHub Release**。先安裝精確鎖定的 dependency closure，再以不解析相依套件的方式安裝 wheel：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-production.txt
uv pip install --python .venv/bin/python --no-deps thinkroom-0.1.0-py3-none-any.whl
.venv/bin/python verify_locked_runtime.py uv.lock --write-manifest runtime-lock-manifest.json
```

一般會自動解析相依套件的 wheel 安裝方式適合評估，但不屬於 locked production closure。經 release 授權的部署方式，是讓已安裝的 Python package 以 native POSIX process 在 Linux 執行；Windows 則在 WSL 內執行。

## 維運與安全邊界

Thinkroom v0.1 刻意維持單節點架構：

- production 預設只綁定 literal loopback；
- SQLite 只支援一個 service instance；
- database parent 必須事先存在於 Linux/POSIX filesystem，擁有者須為有效 service user（或 root），且不可讓 group/world 寫入；
- v0.1 沒有 authentication、RBAC 或 multi-tenancy；
- 若要開放更廣的存取範圍，必須另設安全且經過驗證的 authenticated reverse proxy。

Docker 是由 operator 自行負責的參考資料，不屬於 v0.1 native release claim。若要使用，必須只發布在 host loopback，例如 `-p 127.0.0.1:8787:8787`，並完成 [Operations](docs/OPERATIONS.md) 中的 hardening checklist。

## 驗證

安裝 wheel 後執行：

```bash
python scripts/smoke_package.py
python scripts/smoke_process.py
```

Repo contributor 應執行 [AGENTS.md](AGENTS.md) 所列的完整 gates。

## 專案文件

- [產品規格](docs/specification.md)
- [維運說明](docs/OPERATIONS.md)
- [安全政策](SECURITY.md)
- [架構決策：modular monolith](docs/adr/0001-modular-monolith.md)
- [架構決策：native process release authority](docs/adr/0002-native-process-release-authority.md)

## 授權

Thinkroom 採用 [MIT License](LICENSE) 開源。
