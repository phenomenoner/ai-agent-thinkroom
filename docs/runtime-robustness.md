# Prime backend 升級、逾時診斷與運行優化

本方案保持 Thinkroom 的獨立 rollout、delayed critique、evidence verification、partial 語意與 invocation-local RLM 邊界。目標是提高可診斷性，並讓後續效能調整可由實際紀錄驗證。模型的研究內容正確性仍需獨立確認。

## 版本與相容性

選定 Prime Agent **0.9.8 stable**，upstream commit `7d442aafa985f9342134fac16c2ef41f03fb45c1`。截至 2026-10-06，0.9.9 屬於 beta，不納入這次穩定版升級。

相較 0.9.4，與服務運行最相關的 upstream 改善包括：

- 0.9.5：改用具名的 `await rlm.spawn(task, name=...)`；修復部分 context overflow／compaction recovery，並改善 reasoning 與 prompt cache 的保留。
- 0.9.6：降低 child preview 與 roster event flood 的 CPU 成本；修復 stale worker socket、bridge EPIPE、worker 復原時的不確定命令重播；改善 kernel stderr／bootstrap、child deletion／collect 與 daemon incident timeline。
- 0.9.7：大型 kernel output 的 host reader 改為線性時間，避免反覆掃描 partial line 所造成的停滯。
- 0.9.8：修復 Codex subscription model discovery 過舊版本識別，使 RLM／`find_models()` 能看到目前的 Sol／Luna；model picker 也會重新整理 catalog。

來源：[0.9.8 Release](https://github.com/PrimeIntellect-ai/prime-agent/releases/tag/v0.9.8)、[固定版本 changelog](https://github.com/PrimeIntellect-ai/prime-agent/blob/v0.9.8/packages/coding-agent/CHANGELOG.md)。以上為 upstream 的修正範圍，尚不能單憑 changelog 推論某個歷史任務的根因已消失。

Thinkroom prompt 使用 `rlm.spawn`、保留原始 admission handle，並維持 child identity／reply／cleanup／terminal JSON 的檢查。採用 upstream Linux 原生 artifact，驗證 GitHub 公布的 SHA-256 後安裝到獨立版本目錄；既有版本保留為 rollback。SQLite schema 不因診斷欄位而改版。這是本機整合修改，並不表示已發布新的 Thinkroom Release。

## 歷史證據與限制

2026-10-06 的 read-only SQLite snapshot 保留 29 個任務、161 次 physical provider calls、27 個 failed branches。任務狀態為 15 failed、5 cancelled、9 succeeded；9 個 succeeded 中只有 **2 complete、7 partial**。這是 retention window，不能當成 lifetime 成功率。

| route／model | calls | validated | timeout |
| --- | ---: | ---: | ---: |
| OpenRouter GLM 5.3 Flash | 46 | 12 | 23 |
| Codex Terra，歷史設定 | 93 | 62 | 1 |
| Codex Luna，目前 fallback | 22 | 13 | 3 |

不同 route 對應不同日期、context、phase 與 workload，不能據此做受控模型品質或成本排名。Timeout 是 censored observation；成功 call 的 latency 也不能代表全部 request 的分布。

其他已觀測失敗包括 context admission、output transport limit、schema／JSON、provenance 及不足的 deadline。歷史沒有 RLM 分段 timing；不能斷言所有 timeout 都是 provider 慢，或用重啟成功當成根因證明。

## 已實作的診斷契約

`BackendTransportMetrics` 增加純整數的 monotonic offset 與 counter。stage 表示「最後觀測到的邊界」：

| stage | 邊界 | 可分析的問題 |
| ---: | --- | --- |
| 0 | invocation 已進入，RPC prompt 尚未送出 | process／daemon startup |
| 1 | prompt 已送出 | command admission、worker bridge |
| 2 | RPC prompt 已被接受 | parent model／kernel／child admission |
| 3 | 觀測到相符 child lifecycle | child 執行、reasoning、event flood |
| 4 | 相符 child reply 已觀測 | parent continuation、cleanup 等待 |
| 5 | identity-bound cleanup 已確認 | parent final JSON／terminal turn |
| 6 | terminal JSON 已通過 transport parsing | 後續 schema／provenance validation |

stage 6 不代表 phase schema、research correctness 或整個 job 成功。`elapsed_ms=0` 的 legacy transport metrics 沒有 timing 證據，不能當成 startup stall。缺少或初次 milestone 為零時，需以 stage、elapsed 與 counter 一起判讀。

額外欄位包括 first event、prompt acceptance、child admission／reply、cleanup、terminal offset、last event、maximum wire idle gap，以及 assistant turns、tool calls、child updates、stderr byte count。`assistant_turns` 是 parent RPC 的 assistant message-end 數量，包含 tool turns；不是整個 parent／child family 的 provider request 或 token 使用量。

Process adapter 使用同一個 pipe reader 傳遞 stage change 與每 30 秒的 progress，單次 invocation 最多 128 個 observations。Progress 不會被當成 result；格式／數值錯誤仍 fail closed。Outer route timeout 會保留最後已接收的 observation，標示 `BACKEND_TIMEOUT_ROUTE`，並先完成原有 process containment 再進入 fallback。最後 observation 的 elapsed 不是精確的 timeout 時間或 daemon／child stop 證明。

每個有 metrics 的 physical call 以 `provider_diagnostics` artifact 保存 call ID、phase、branch、outcome 與 metrics，可與既有 provider_calls join。診斷寫入是 best effort：cancellation／stale attempt／artifact budget 不可被診斷失敗改寫；突然終止的 call 可由 journal progress 補充。每次新 call 的 artifact 與既有 job retention 一起管理。

Structured journal 補上 branch／call、admission wait、剩餘 deadline、完整 rendered prompt／RPC command byte 數、effective startup configuration、schema validation error 類型與數量。禁止保存 prompt、context、provider response、raw stderr、任意 exception message、credential 或完整 SDK transcript。Validation error log 不帶 input、context 或 rejected value。

## Robust 運行策略

1. **先修已知上游問題，再觀測新樣本。** 升級前保留 exact runtime／backend／unit；以無 inference 的 RPC startup、protocol fixtures、package install、native process／restart／exclusive lock 驗證。正式 instance 切換必須確認沒有 active jobs，且新版本與既有 daemon 不共用 invocation socket。
2. **將 timeout 與 admission／格式／provenance 分開。** 遇到長 context 先使用完整 rendered JSONL byte accounting；不以截斷內容、放寬 provenance 或重播失敗 job 來製造成功。
3. **Deadline 按 workload 與完整 phase chain 配置。** 目前 300 秒 primary、600 秒 fallback、4500 秒 hard／3600 秒 soft 是有上限的設定。兩 branches 有六個必要 phase calls；一旦 primary timeout 開啟 attempt circuit，即使之後只用 fallback，仍需保留 critique／synthesis 與允許的 schema repair budget。短 request deadline 不能保證完整研究。先看 route／phase p95、queue wait、repair 次數，再調整。
4. **保留 bounded retry／fallback。** Timeout 不做同 route 重試；每個 phase／branch 最多三次 physical calls；schema repair 保持 producer affinity。Unknown outcome 保留 custody，不重播歷史任務；已知取消／deadline／limit error 仍遵守既有 terminal policy。
5. **以 content-free evidence 決定 early timeout。** Stage 0／1 持續停滯可支持 startup／admission 的獨立上限；stage 3 的 wire 活動可能只是重複 snapshot，不能直接當成有效研究進度。目前先收集 log，不根據 idle gap 自動殺掉仍在工作的模型。
6. **調整並行度前維持 branch isolation。** 現在一個 job、一個 rollout slot。只有 queue wait 明顯吃掉 deadline，且 native child／socket／cleanup 證據足夠，才比較兩個 rollout slots；不得共享 kernel 或工作目錄。
7. **服務更新採單一 owner 與 quiescent boundary。** 以 deployment-owner lock、exact unit/config preimage、零 active jobs、獨立 runtime、一次 service restart 與 readback 控制切換。未產生新 external effects 時可恢復 exact 前一組 runtime/backend；新 job 已有 effects 後不得恢復舊 DB snapshot。

## 後續優化的觸發條件

| 訊號 | 下一個受控實驗 | 停止／保留條件 |
| --- | --- | --- |
| startup／acceptance 常佔主要耗時 | 分離 daemon/kernel cold start deadline；比較已準備 runtime | 最多兩個小樣本；不知道 command outcome 時不重播 |
| primary 同類 timeout 跨 jobs 重複 | opt-in、有限 cooldown 加單一 half-open probe | 保留 route identity；不能把 quota、格式或 capacity error 混為同一 circuit |
| parent turns／child updates 與 wire bytes 放大 | 收集 token usage、child activity 與 meaningful progress；再比較 reasoning／output guidance | 不改用未授權 model／provider；不將 wire event 數當成 tokens |
| repair 佔大量 latency | 以 validation error 類型改進 schema guidance／最小 context | 不放鬆 provenance 與 evidence verification |
| queue wait 超過可用 execution budget | 比較兩個 rollout slots或降低 job admission | 先驗證 cancellation、cleanup、後續 job 及 rate limits |
| 長 context 不成比例地失敗 | explicit evidence packet／source refs，按需求分段輸入 | 不默默省略必要 evidence；保留原始問題與缺口 |

這些是待驗證的後續實驗，未自動改變 route、reasoning、並行度或 live deadline。重設所有 timeout、加入分散式 infrastructure 或無上限 retry 都不是目前證據支持的做法。

## 重新分析

```bash
python3 scripts/analyze_runtime_history.py \
  --database /absolute/path/to/thinkroom.db \
  --since 2026-10-01T00:00:00+00:00 --limit 1000
```

工具在一個 read-only transaction 中輸出 retention window、states、complete／partial、terminal codes、failed branches、context-size buckets、route／phase latency、runtime-diagnostics coverage 與 timeout 最後觀測 stage。不輸出研究文字；percentiles 使用 nearest rank，validated latency 與所有已結束 call 的 latency 分開計算。若樣本被 limit 截斷，JSON 明示 `truncated`。

用既有 journal service selector 讀取 `provider_runtime_progress`、`provider_runtime_finished`、`provider_admission_acquired`、`provider_invocation_failed`，以 job／attempt／correlation／phase／branch join。保留 journal retention；需要長期存檔時只 export 已通過內容白名單的 JSON event。
