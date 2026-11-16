# 透過 Trino 使用 Apache Ossie：可行架構與落地方式

> 研究日期：2026-11-15  
> 範圍：Apache Ossie、Trino，以及 Trino 後端若要連 StarRocks 時的邊界。本文只引用官方文件、官方原始碼庫與官方規格。

## 結論先講

目前不能把 Apache Ossie 當成 Trino 外掛直接安裝，也沒有官方的 Ossie connector 可以讓 Trino 直接讀取 Ossie YAML/JSON 後自動提供 metrics 查詢。

現在最可行的做法是把 Ossie 放在 Trino 前面，做成一個「語意模型驗證器＋SQL 編譯器／查詢閘道」：

```text
BI／AI／應用程式
       │  語意查詢：metrics、dimensions、filters
       ▼
Ossie 語意服務
  1. 讀取並驗證 Ossie YAML／JSON
  2. 解析資料集、欄位、關聯、metric 與 grain
  3. 規劃安全 join 與預先彙總
  4. 產生 Trino SQL
       │  JDBC 或 POST /v1/statement
       ▼
Trino
       │  各式 catalog／connector
       ▼
Iceberg／Hive／PostgreSQL／MySQL／其他資料來源
```

### 本專案的落地狀態

本儲存庫已依上述架構加入第一版 Trino target。它和 StarRocks target 共用同一套 Ossie 模型驗證、relationship graph、grain planner 與 Layer 3 filter placement，再將已證明安全的 query plan 輸出成普通 Trino SQL：

```python
result = compiler.compile(query, target="trino")
```

CLI 對應為 `ossie-trino compile ... --target trino`。GitHub 的 `live-trino` workflow 會在官方 Trino 容器內，把產生的 SQL 和手寫 baseline SQL 對同一份 TPCH 資料執行並比較結果。這是外部 semantic compiler PoC，不代表 Trino 已原生支援 Ossie，也不會改變下文對官方整合狀態的判斷。

儲存庫也提供 `ossie-trino query`、`TrinoClient` 與最小的 `ossie-gateway`。它們可將同一條已驗證、唯讀的 `SELECT`／`WITH` SQL 透過 Trino `/v1/statement` client protocol 執行、沿 `nextUri` 讀完結果，並回傳 query ID、欄位 metadata 與資料列。Gateway 固定載入一份模型，只接收 Layer 3 request JSON，要求 Bearer token，並限制 request body、執行時間與結果列數。這讓 PoC 涵蓋「HTTP 語意 API＋編譯＋執行＋基本稽核關聯」，但仍不是完整的多租戶平台；正式環境還要補模型級／欄位級授權、資源群組、持久化 audit log、TLS 與 identity-aware proxy。

理由如下：

- Ossie 是交換語意模型的 JSON/YAML 規格，不是查詢引擎。官方 README 將它定位為跨 AI、BI 與分析工具交換語意模型的 vendor-neutral specification。[Apache Ossie README](https://github.com/apache/ossie/blob/main/README.md)
- Ossie 的 portable expression language 明確區分「模型宣告」與「查詢」；登記某個 dialect 並不代表 Ossie 已支援用該 dialect 查詢。[Ossie Expression Language：Model Declaration vs. Querying](https://github.com/apache/ossie/blob/main/core-spec/expression_language.md#model-declaration-vs-querying)
- 官方 dialect catalog 已列出 `TRINO`，但同一份文件也說，除了 `OSSIE_SQL_2026` 之外，其他 dialect 對 Ossie 都是 opaque token。因此 `dialect: TRINO` 只能表示該運算式應由理解 Trino SQL 的 consumer 處理，不能自動把 Trino 變成 Ossie 引擎。[Ossie Dialect Extensions Catalog](https://github.com/apache/ossie/blob/main/extensions/dialects.md)
- Ossie 的標準語意查詢介面與 reference engine 仍在演進。官方 roadmap 將 semantic query language、query plan 與 Ossie-to-SQL reference compiler 列為後續交付項目；Relational Query Interface 目前也標示為 draft。[Ossie Roadmap](https://github.com/apache/ossie/blob/main/ROADMAP.md#semantic-query-language--reference-engine)、[Relational Query Interface](https://github.com/apache/ossie/blob/main/core-spec/relational_semantics.md)

### 截至 2026-11-15 的 `TRINO` dialect 狀態

這裡有一個容易誤判的細節：

- Apache Ossie PR [#607「add TRINO to Dialect enum」](https://github.com/apache/ossie/pull/607) 截至研究日仍列在 **Open pull requests**，所以不能把這支 PR 當作已合併的正式支援依據。[Apache Ossie open PR 清單](https://github.com/apache/ossie/pulls)
- 同一時間，`main` 的非規範性 extension catalog 已經列出 `TRINO`。[Dialect Extensions Catalog](https://github.com/apache/ossie/blob/main/extensions/dialects.md)
- 兩件事並不矛盾，也不代表已有 Trino runtime：目前 core spec 把 dialect 欄位當自由字串；catalog 只是社群使用的 token 登記表，而且明說 `OSSIE_SQL_2026` 以外的 dialect 對 Ossie 都是不透明值。換句話說，模型可以標記一段 expression 是 Trino SQL，但仍要由外部 consumer 自行解析、驗證、轉譯或執行。

因此本文採用的判斷是：**`TRINO` 可作為模型中的 vendor dialect 標記，但沒有證據顯示 Apache Ossie 或 Trino 已提供正式的 Ossie→Trino 查詢執行整合。**

## Ossie Layer 2／Layer 3 對 Trino 整合的意義

官方 `spec_layers.md` 是 informational architecture overview，而且仍是 draft；它把 Ossie 分成四層，其中與 Trino 直接相關的是：[Ossie Specification Layers](https://github.com/apache/ossie/blob/main/core-spec/spec_layers.md)

| 層級 | 官方定位 | 對 Trino 整合的實際意義 |
|---|---|---|
| Layer 1 | Model Definition：模型格式與模型內 expression language | Trino 前面的服務讀取 Ossie YAML/JSON，並把 expression 映射成 Trino SQL。這一層本身不定義查詢介面。 |
| Layer 2 | Relational Query Interface：SQL-native、以 measure type 與 `AGG()` 避免 fan-out／chasm trap | 最接近「Trino 原生 Ossie」的長期方向；但 Trino 目前沒有 Ossie measure type 或 `AGG(measure)` 語意，單靠 connector 或 UDF 無法宣稱符合 Layer 2。 |
| Layer 3 | Wide Table Query Interface：client 只指定 dimensions、metrics 與 filter，由服務決定 join | 最適合近期落地成 Trino 前方的 semantic compiler／gateway；它可把宣告式請求規劃成多階段 Trino SQL。 |

Layer 2 與 Layer 3 可以組合：官方架構說 Layer 3 的結果要等價於 Layer 2 的 SQL-measures query，但不要求實作者真的先產生 Layer 2 SQL 文字。因此近期實作可以直接把 Layer 3 請求編譯成普通 Trino SQL，只要結果符合等價語意；不必等待 Trino 原生支援 `AGG()`。[Ossie Specification Layers：Composition](https://github.com/apache/ossie/blob/main/core-spec/spec_layers.md#composition)

這也改變了實作優先順序：

1. **近期**：實作 Layer 3 風格的外部 API 與安全 planner，輸出普通 Trino SQL。
2. **中期**：用 Ossie Layer 2 的 duplication-free measure semantics 當正確度規格與 conformance 目標。
3. **長期**：只有 Trino 本身或深度 plugin 真正支援 measure type、`AGG()` 與 discovery metadata 時，才可稱為 SQL-native Layer 2 integration。

## 建議方案：Ossie-to-Trino SQL 編譯服務

### 1. 模型載入與驗證

語意服務載入一份 Ossie 文件，先做 schema 與語意驗證。官方 Python package 提供 Pydantic v2 models，可解析、驗證與序列化 Ossie 文件；它是模型工具，不是 SQL 執行器。[Apache Ossie Python package](https://github.com/apache/ossie/blob/main/python/README.md)

最低限度應檢查：

- Ossie 文件版本與 schema。
- dataset 的 `source` 是否可對應到 Trino 的三段式名稱 `catalog.schema.table`。
- field／metric 運算式使用 `OSSIE_SQL_2026` 或 `TRINO` 時，編譯器是否真的支援該語法。
- relationship 的鍵、方向、唯一性與 cardinality 是否足以判斷 join 會不會造成 fan-out。
- metric 的來源 grain、filter scope、跨 dataset metric 是否需要各自預先彙總。

### 2. 接收語意查詢，不直接接收任意 SQL

建議由 client 傳入結構化查詢，例如：

```json
{
  "metrics": ["orders.revenue"],
  "dimensions": ["customers.country"],
  "filters": [
    {"field": "orders.order_date", "op": ">=", "value": "2026-01-01"}
  ],
  "limit": 100
}
```

語意服務再依 Ossie 模型決定需要哪些資料集、join path、filter 放置位置和彙總層級。這一層不能只做字串取代，因為官方的 Relational Query Interface 特別指出，普通 SQL 在多表 join 下會發生 fan-out／chasm trap；各 metric 必須在自己的來源資料列上計算，才不會重複加總。[Ossie Relational Query Interface §1.1–1.2](https://github.com/apache/ossie/blob/main/core-spec/relational_semantics.md#11-purpose)

### 3. 編譯成 Trino SQL

單一事實表、many-to-one 維度的查詢可以直接產生：

```sql
SELECT
    c.country,
    SUM(o.amount) AS revenue
FROM lake.sales.orders AS o
LEFT JOIN lake.sales.customers AS c
    ON o.customer_id = c.customer_id
WHERE o.order_date >= DATE '2026-01-01'
GROUP BY c.country
LIMIT 100
```

若同一個結果包含不同 grain／不同事實表的 metrics，應先各自彙總，再用維度鍵合併：

```sql
WITH revenue_by_country AS (
    SELECT c.country, SUM(o.amount) AS revenue
    FROM lake.sales.orders o
    JOIN lake.sales.customers c ON o.customer_id = c.customer_id
    GROUP BY c.country
),
customer_count_by_country AS (
    SELECT country, COUNT(*) AS customer_count
    FROM lake.sales.customers
    GROUP BY country
)
SELECT
    COALESCE(r.country, c.country) AS country,
    r.revenue,
    c.customer_count,
    r.revenue / NULLIF(c.customer_count, 0) AS revenue_per_customer
FROM revenue_by_country r
FULL OUTER JOIN customer_count_by_country c
    ON r.country IS NOT DISTINCT FROM c.country
```

這種多階段 SQL 是對 Ossie 正確度要求的實作推論，不是目前官方提供的 Trino compiler。Trino 本身可執行標準 aggregate、`HAVING` 與 aggregate `FILTER (WHERE ...)`；但函式、型別、日期截斷、近似聚合與 NULL 行為仍需逐項做 dialect mapping。[Trino Aggregate Functions](https://trino.io/docs/current/functions/aggregate)、[Trino SELECT / HAVING](https://trino.io/docs/current/sql/select.html#having-clause)

### 4. 將 SQL 送進 Trino

有兩條官方支援的 client 路徑：

1. **JDBC**：使用 `io.trino.jdbc.TrinoDriver`，連線字串為 `jdbc:trino://host:port/catalog/schema`。[Trino JDBC driver](https://trino.io/docs/current/client/jdbc.html)
2. **HTTP client protocol**：對 `/v1/statement` 送出 SQL，再沿回應的 `nextUri` 逐批取回結果；官方仍建議一般應用優先使用既有 client library。[Trino client REST API](https://trino.io/docs/current/develop/client-protocol.html)

正式環境還需要把 Ossie model version、產生的 SQL、Trino query ID、執行者與查詢參數串在同一筆 audit record，才能重現「某一版 metric 定義算出哪個結果」。

## 可選方案

### 方案 A：將穩定資料集發布成 Trino views

編譯器可把 row-level 欄位或固定 grain 的 metric 產生為 `CREATE OR REPLACE VIEW`。Trino 的 view 是儲存的查詢，每次引用時重新執行，也支援 `SECURITY DEFINER` 與 `SECURITY INVOKER`。[Trino CREATE VIEW](https://trino.io/docs/current/sql/create-view.html)

適合：

- 清理欄位名稱與型別。
- 固定、單一 grain 的彙總資料集。
- 需要讓既有 BI 工具直接看到 table/view 的情境。

不適合拿來宣稱完整 Ossie 語意相容，因為普通 view 無法在任意下游 join、任意 grouping 下，自動保證每個 metric 都維持自己的來源 grain。若把 raw aggregate metric 直接塞進可任意 join 的 view，仍可能重複計算。

### 方案 B：做 Trino 原生 plugin／connector

Trino 的 SPI 允許自訂 plugin 與 connector；connector 可提供 schema、table、column metadata，也能實作 filter、projection、aggregation 與 join pushdown。[Trino SPI overview](https://trino.io/docs/current/develop/spi-overview.html)、[Trino connector development](https://trino.io/docs/current/develop/connectors.html)

理論上可做一個 Ossie connector，把 model/dataset 暴露成 Trino catalog；但這不是短期捷徑：

- 必須自行定義 virtual table、split 與 page source，或把查詢安全地下推到真正資料來源。
- Ossie 跨 dataset 的 metric grain、relationship resolution 和 fan-out 防護仍需自行實作。
- connector SPI 的主要抽象是「連接外部資料來源」，不是替 Trino SQL parser 增加 Ossie 的 `AGG(measure)` 型別語意。
- Ossie Layer 2 的 `AGG()` 語法與 measure semantics 目前仍是 draft，官方文件也說範例拼法是 provisional。[Ossie Relational Query Interface §1.3](https://github.com/apache/ossie/blob/main/core-spec/relational_semantics.md#13-scope)

因此只有在需要讓所有 Trino client 都「像查 catalog 一樣」存取 Ossie，而且願意長期維護 Java plugin 與 Trino 版本相容性時，才值得走這條路。第一版仍建議採外部編譯服務。

## Trino 後端若是 StarRocks

這段要特別保守：截至研究日，Trino 官方 connector 清單沒有 StarRocks connector。Trino 的 MySQL connector 官方需求是 MySQL 5.7、8.0 或以上，官方沒有宣告 StarRocks 相容；即使 StarRocks 提供 MySQL-compatible protocol，也不能直接視為受 Trino 官方支援。[Trino MySQL connector](https://trino.io/docs/current/connector/mysql.html)

可選路徑依風險排序：

1. **如果主要執行引擎就是 StarRocks，直接把 Ossie 編譯成 StarRocks SQL。** 少一層 dialect、型別與 pushdown 差異，通常最穩定。
2. **若一定要經過 Trino，開發 StarRocks 專用 Trino JDBC connector。** Trino 官方提供以 `BaseJdbcClient` 擴充新 JDBC 資料來源的範例；StarRocks 官方也提供自己的 JDBC driver。這條路需要做型別映射、predicate／aggregate／join pushdown 與整合測試。[Trino Example JDBC connector](https://trino.io/docs/current/develop/example-jdbc.html)、[StarRocks JDBC Driver](https://docs.starrocks.io/docs/integrations/JDBC_driver/)
3. **用 Trino MySQL connector 連 StarRocks只可當實驗性 PoC。** 必須實測 metadata、型別、時間、decimal、NULL、字串比較、prepared statement 與 pushdown；不能因為連得上就當成語意正確或正式受支援。

反方向的「StarRocks 用 JDBC catalog 連 Trino」也不是官方列出的支援組合。StarRocks 的 JDBC catalog 文件目前列出的 resolver／支援來源是 MySQL、PostgreSQL、Oracle、SQL Server 與 ClickHouse，未列 Trino。[StarRocks JDBC catalog](https://docs.starrocks.io/docs/data_source/catalog/jdbc_catalog)

## 建議落地階段

### 第 1 階段：外部編譯器 PoC

- 鎖定一個 Ossie schema 版本；不要跟著 `main` 未發布 draft 靜默變動。
- 只接受 `OSSIE_SQL_2026` 的可攜子集合，或明確接受 `TRINO` 並把它視為 Trino-specific expression。
- 支援單 dataset metric、many-to-one dimension、WHERE、GROUP BY、HAVING 與 limit。
- 透過 Trino JDBC 執行，記錄 SQL 與 query ID。
- 對每一筆測試資料同時計算手寫 baseline SQL，比對結果。

### 第 2 階段：正確的跨 grain metric

- 建立 relationship graph 與 cardinality 驗證。
- 每個 metric 先在自己的 grain 彙總。
- 依共同維度鍵用 null-safe join 合併 stage。
- 對不安全的一對多反向 traversal、歧義 join path 與無法解析的 expression 採 fail closed。

### 第 3 階段：產品化

- 加入模型版本、權限、查詢 audit、timeout、資源群組與 cache key。
- 對外提供穩定的 semantic query API。
- 視 BI 工具需求，另外發布安全的 Trino views；不要用 views 取代跨 grain planner。
- 只有在必須讓任意 Trino client 直接探索 Ossie catalog 時，再評估 Java connector/plugin。

## 驗證清單

至少要以小型固定資料集驗證下列案例：

- 同一 metric 在「不分組」與「依維度分組」下等於手寫 Trino SQL。
- fact → dimension 的 many-to-one join 不改變 metric 總額。
- 拒絕會讓 metric fan-out 的 one-to-many 反向 join，或改寫成先彙總再 join。
- 兩個不同 grain metrics 的比例，在全域與依共同維度分組時都正確。
- `COUNT(DISTINCT ...)`、aggregate `FILTER (WHERE ...)`、NULL、零除、decimal 精度與日期／時區行為一致。
- filter 只被推到語意上可安全到達的 stage；metric filter 與 query-time filter 不互相覆蓋。
- 使用 `EXPLAIN` 檢查 connector pushdown，但正確度不能依賴「應該會 pushdown」。Trino connector 可以拒絕某次 pushdown，讓引擎自行完成剩餘運算。[Trino connector pushdown contract](https://trino.io/docs/current/develop/connectors.html)

## 最後判斷

「透過 Trino 使用 Apache Ossie」是可行的，但現階段的正確說法是：**自行建置 Ossie semantic compiler／gateway，以 Trino 當聯邦 SQL 執行引擎**，而不是「在 Trino 安裝 Ossie」。

最小可行產品應先走外部編譯服務；Trino view 可當發佈介面，但不能取代跨 grain planner；Trino plugin 是長期整合選項，不是必要起點。如果底層唯一資料庫是 StarRocks，直接產生 StarRocks SQL 通常比繞過 Trino 更簡單，除非確實需要 Trino 的多 catalog 聯邦查詢能力。
