# Autoresearch

[English](../../README.md) | **日本語**

Codex のための、自律的で測定可能な実験ループです。

数値目標を伝えると、Codex はリポジトリを調査して実行条件を確認し、1 つ変更、検証、改善の保持、失敗の取り消しを目標達成まで繰り返します。

テスト失敗数、カバレッジ、型エラー、警告、レイテンシ、バイナリサイズ、再現可能なセキュリティ検出などに使えます。

## クイックスタート

Codex でインストールします。

```text
$skill-installer install https://github.com/leo-lilinxiao/codex-autoresearch
```

クリーンな Git リポジトリを Full Access で開くことを推奨します。

```bash
codex --dangerously-bypass-approvals-and-sandbox
```

次に実行します。

```text
$autoresearch `python3 scripts/score.py` の error_count を 0 にする
```

最初の書き込み前に、目標、変更範囲、ベースライン、ターゲット、測定コマンド、任意の guard、並列数 を確認します。

## 仕組み

```text
証拠を確認 -> 1 つの仮説を変更 -> コミットして測定
                                      |
                         改善 + guard 成功: 保持
                         それ以外: git revert
                                      |
                               記録して継続
```

Codex が仮説とコード変更を担当し、制御スクリプトが Git 境界、測定、ロールバック、状態を担当します。

## 並列候補

| | |
|---|---|
| 分離 | スロットごとに 1 つの長寿命 Git ワークツリー |
| 割り当て | 最良結果の深掘りと新しい着想の試行を適応的に配分 |
| 計算資源 | 宣言されたコアとマシンのバンク。各候補に割り当てを付与 |
| 採用 | 直列化。基点が古い候補はリベースして再測定 |
| 生存確認 | リース方式。制御プレーンはワーカープロセスを所有しないため |

各ワーカーは同じ全体目標と整備済みの決定事項、そして自身の個別目標を受け取ります。並行サブエージェントを起動できないホストでは 1 スロットずつ確保し、同一の状態モデルのまま逐次実行に縮退します。

## 結果

未コミットの `autoresearch-results/` に保存されます。

| パス | 内容 |
|---|---|
| `run.json` | 確認済みの不変設定 |
| `events.jsonl` | 追記専用の状態・監査履歴 |
| `logs/` | 測定、guard、worker の完全な出力 |
| `slots.json` | スロットの生存状態、リース、割り当て中の計算資源 |
| `docs/` | 整備済みドキュメントのスナップショット |

`events.jsonl` が唯一の実行状態です。欠損、破損、矛盾があれば推測で復旧せず、明確に失敗します。

## 履歴とレポート

```text
$autoresearch show experiment history
$autoresearch export experiment history as TSV
$autoresearch generate an HTML report
```

履歴表と HTML は検証済みイベントから生成されます。HTML スナップショットは `autoresearch-results/report.html` に保存され、実行状態や復旧には使用されません。

## 信頼性

- 新しい実行にはクリーンな名前付き Git ブランチが必要です。
- 1 実行は 1 リポジトリ、1 指標、1 ターゲットです。
- 各実験はコミットされ、失敗は `git revert` されます。
- 範囲外変更、Git ドリフト、不正な指標、コマンド失敗、タイムアウト、ロールバック失敗はログ付きで停止します。
- 保持された指標がターゲットに達した場合だけ `complete` になります。

## 要件

- Skills と Goals を備えた現行 Codex CLI
- Python 3.11+
- Git

[インストール](../INSTALL.md)、[ユーザーガイド](../GUIDE.md)、[例](../EXAMPLES.md)も参照してください。

MIT License。着想は [Karpathy の autoresearch](https://github.com/karpathy/autoresearch) から得ています。
