# ETL Generator — Implemented Improvements

---

## Fix 1 — `transaction_id` vazio resolvido para chave composta

**File:** `pipeline/transform.py`

**Before / Antes:**
```python
# linha 386
"transaction_id": raw_txn_id,   # vazio quando ARCOS não tem order_form_no
```

**After / Depois:**
```python
"transaction_id": txn_id,       # chave composta: reporter|buyer|date|drug
```

**Por que foi feito:**
Cerca de 50% das linhas do ARCOS não possuem `order_form_no` preenchido. O código já construía
uma chave composta determinística (`txn_id`) para usar como semente do SSCC, PO number e SGTINs,
mas gravava o valor original vazio (`raw_txn_id`) na coluna `transaction_id` do CSV. Com isso,
o campo `ilmd.cbvmda:transactionID` nos eventos EPCIS gerados por `load.py` ficava como
`"row-N"` — um fallback arbitrário que não coincidia com a chave usada para os identificadores
do lote. As regras do chaincode que correlacionam eventos pelo `transactionID` (como
`LotContaminationRule` e `ShipmentReceiptDivergenceRule`) deixavam de funcionar para esses lotes.

Agora `transaction_id` no CSV sempre reflete a chave que foi usada para gerar todos os outros
identificadores do lote.

---

## Fix 2 — `reporter_id` incluído como `possessing_party` no evento de shipping

**File:** `pipeline/load.py`

**What changed:**
1. Após ler `reporter_id` em `events_for_lot()`, deriva-se o PGLN correspondente:
   ```python
   reporter_source = (
       [{"type": "possessing_party", "source": dea_to_pgln(reporter_id)}]
       if reporter_id else []
   )
   ```
2. O closure `evt()` recebeu um parâmetro `extra=None` para aceitar entradas adicionais no
   `sourceList`.
3. Todas as chamadas `evt("shipping", ...)` em todos os ramos de anomalia passam
   `extra=reporter_source`.

**Por que foi feito:**
`reporter_id` é o número DEA do distribuidor/vendedor registrado no ARCOS. Ele era extraído pelo
`extract.py`, passado pelo CSV, lido em `events_for_lot()` — e então nunca usado. Nenhum campo
de nenhum evento carregava a identidade do remetente, tornando o vendedor invisível no ledger do
blockchain.

O evento de `shipping` é o momento em que o vendedor transfere a custódia do produto para o
comprador — semanticamente é o lugar correto para registrar o remetente como parte possuidora.
Após essa mudança, qualquer análise forense no ledger consegue identificar quem expediu cada lote.

**Exemplo de entrada gerada no `sourceList` de um shipping ObjectEvent:**
```json
{
  "type": "possessing_party",
  "source": "urn:epc:id:pgln:us.dea.RA0000001"
}
```

---

## Fix 3 — `quantity_grams` removido do CSV de saída

**File:** `pipeline/transform.py`

**What changed:**
- Removido de `output_columns` (era o 6º item, entre `drug_code` e `dosage_unit`)
- Removido do dict `enriched_rows` dentro de `build_scenario()`

`extract.py` continua extraindo o campo `calc_base_wt_in_gm` do ARCOS para `arcos_clean.csv` —
esse arquivo intermediário não foi alterado.

**Por que foi feito:**
`quantity_grams` é o peso em gramas do princípio ativo, uma métrica específica do ARCOS/DEA. O
padrão EPCIS 2.0 rastreia contagem de unidades (`dosage_unit` → `lot_size`), não peso. O campo
nunca era lido por `load.py` e nenhuma regra do chaincode usa peso. Mantê-lo no CSV de saída
criava uma falsa expectativa de que o peso influenciaria algum resultado de detecção.

---

## Fix 4 — `many_suppliers` removido do `ANOMALY_CYCLE`; `extra_distributor_ids` eliminado

**Files:** `pipeline/transform.py`, `pipeline/load.py`

### transform.py

**What changed:**
- `"many_suppliers"` substituído por `"none"` no `ANOMALY_CYCLE` (com comentário explicativo)
- `"extra_distributor_ids"` removido de `output_columns`
- Entrada `"extra_distributor_ids"` removida do dict `enriched_rows`
- Funções `make_extra_distributors()` e constante `EXTRA_DIST_COUNT` **mantidas** com comentário —
  são a implementação do limiar de Skilton (2024) e devem permanecer para quando a regra for
  reativada

### load.py

**What changed:**
- Bloco de injeção de `extra_sources` removido (era a inicialização do loop sobre
  `extra_distributor_ids` que populava `extra_sources`)
- A variável `extra_sources = []` foi mantida como placeholder comentado para uso futuro

**Por que foi feito:**
`SupplyBaseComplexityRule` está comentada em `RuleEngine.js` — há um `TODO` aberto sobre se o
limiar de Skilton et al. (2024) (≥9 fornecedores = 99º percentil) é válido para o contexto EU→US
deste estudo. Com a regra desativada, o tipo de anomalia `many_suppliers` gerava eventos com
sequência idêntica à de transações limpas (`"none"`) e `riskScore=0`. O dado de
`extra_distributor_ids` era produzido mas completamente ignorado pelo chaincode.

Substituir `"many_suppliers"` por `"none"` não altera a taxa de anomalia (~30%) porque o
`many_suppliers` já usava o fluxo normal de eventos — o ciclo de 13 entradas continua com
4 `"none"` e 9 anomalias ativas.

---

## Column Schema After Changes

`scenario_template.csv` agora tem **12 colunas** (eram 14):

| # | Column | Fed by rule(s) |
|---|---|---|
| 1 | `transaction_id` | LotContaminationRule, ShipmentReceiptDivergenceRule, QuantityDiscrepancyRule, TransitDiversionRule |
| 2 | `reporter_id` | Agora incluso no `sourceList` do shipping event (ledger record) |
| 3 | `buyer_id` | LogisticsDiversionRule, TransitDiversionRule, VolumeFragmentationRule |
| 4 | `transaction_date` | ShipmentReceiptDivergenceRule (fallback) |
| 5 | `drug_code` | HMAC input para geração de SGTINs |
| 6 | `dosage_unit` | QuantityDiscrepancyRule (via `lot_size`) |
| 7 | `eu_manufacturer_gln` | LogisticsDiversionRule (detecção de jurisdição EU) |
| 8 | `sscc` | QuantityDiscrepancyRule, TransitDiversionRule |
| 9 | `po_number` | ShipmentReceiptDivergenceRule |
| 10 | `anomaly_type` | Meta-coluna do ETL — controla sequência de eventos |
| 11 | `waypoint_dates` | LogisticsDiversionRule (out-of-order), ShipmentReceiptDivergenceRule |
| 12 | `item_mismatch_waypoint_index` | TransitDiversionRule, LogisticsDiversionRule |

**Removidas:** `quantity_grams` (dado morto), `extra_distributor_ids` (regra desativada).

---

## Remaining Known Gap (not fixed here)

A detecção de **jurisdição EU** ainda requer correção no lado do chaincode
(`LogisticsDiversionRule.js:134`), pois `extractJurisdiction()` procura pela substring `eu.` mas
os PGLNs europeus gerados pelo ETL usam prefixos numéricos GS1 (`4000XXX.YYYYY`). Esse fix está
documentado em `../ANALYSIS.md` (Gap 1).
