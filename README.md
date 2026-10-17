# Discord-Flea-Market

Discordサーバーのメンバー同士で売り買いできる **フリマBot** です。
フォームを埋めるだけで出品でき、購入・値下げ交渉・取引ページ（アズカリ）の受け渡し・受け取り報告・相互評価まで、すべてボタン操作で完結します。

## できること

| 機能 | 内容 |
| --- | --- |
| 📦 出品 | 「出品する」ボタン or `/出品` でフォームが開く。商品名・価格・状態（選択式）・説明・画像（最大4枚）を入力するだけ |
| 🛒 購入 | 出品ページの「購入する」→ 確認 → **出品者にDMで購入通知**。商品は自動で「取引中」に |
| 📄 取引ページ送付 | 出品者がDMの「取引ページURLを送る」からアズカリのURLを貼る → **購入者にDMで通知** |
| 📦 受け取り | 購入者がDMの「受け取りました」を押すと取引完了、出品ページは **SOLD（掲載終了）** に |
| ⭐ 相互評価 | 取引完了後、お互いに★1〜5＋コメントで評価（任意） |
| 💬 値下げ交渉 | 希望価格を送る → 出品者は「承諾 / 別の価格を提案 / お断り」。承諾されるとそのまま取引開始 |
| 📊 評価の確認 | 出品ページの「出品者の評価」or `/評価` で平均★・出品者/購入者としての評価・取引実績・最近のコメントを表示 |
| ✖️ キャンセル | 受け取り前なら出品者・購入者どちらからでもキャンセル可（商品は「出品中」に戻る） |
| ⚙️ 出品者メニュー | 出品内容の編集・取り下げ（サーバー管理者も取り下げ可） |

## 取引の流れ

```
Aさん（出品者）                     Bot                         Bさん（購入者）
  │ 出品フォームに入力 ──────▶ 出品ページを投稿
  │                                  │ ◀──────── 「🛒 購入する」
  │ ◀── DM「購入リクエストが届きました」
  │ アズカリで取引ページ作成
  │ 「📄 取引ページURLを送る」 ──▶ ──── DM「取引ページが届きました」──▶ │
  │                                  │            商品到着後「📦 受け取りました」
  │                            出品ページを SOLD に
  │ ◀── DM「取引完了・評価しますか？」 ── DM「取引完了・評価しますか？」──▶ │
  │ ⭐ 評価（任意）                                        ⭐ 評価（任意）│
```

値下げ交渉の場合は「💬 値下げ交渉」→ 出品者にDM →「承諾」で上の「購入リクエスト」から同じ流れになります。

## コマンド

| コマンド | 説明 |
| --- | --- |
| `/フリマ設置` | （管理者）実行したチャンネルをフリマに設定し、「出品する」ボタン付きの案内を投稿 |
| `/出品` | 出品フォームを開く |
| `/評価 [メンバー]` | メンバー（省略時は自分）の評価を見る |
| `/マイ取引` | 自分の出品中の商品と進行中の取引を一覧表示 |

## セットアップ（Render で無料ホスティング）

PCを起動しっぱなしにしなくても、Render（無料）で24時間動かせます。

> **無料プランの注意点と対策（対応済み）**
> - Render 無料プランは **15分アクセスがないとスリープ** → Bot が自分の公開URLに10分ごとにアクセスして起きたままにします
> - Render 無料プランは **再起動・再デプロイでファイルが消える** → データは外部の無料 PostgreSQL（Neon）に保存します
> - 無料枠は月750時間。**Render の無料 Web Service はこのBot 1つだけ**にしてください（1つなら24時間×31日に収まります）

### 1. Discord Bot を作る
1. [Discord Developer Portal](https://discord.com/developers/applications) →「New Application」
2. 「Bot」タブ →「Reset Token」でトークンをコピー（**誰にも見せない**）
3. 「OAuth2 → URL Generator」で以下にチェックし、生成されたURLからサーバーに招待
   - SCOPES: `bot`, `applications.commands`
   - BOT PERMISSIONS: `View Channels`, `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`

### 2. 無料データベース（Neon）を作る
1. [neon.tech](https://neon.tech) に GitHub アカウントでサインアップ
2. 「Create project」→ 名前は自由、Region は `AWS Asia Pacific (Singapore)` など近い場所
3. ダッシュボードの「**Connect**」→ 表示される接続文字列（`postgresql://...` で始まるもの）をコピー

### 3. Render にデプロイ
1. [render.com](https://render.com) に GitHub アカウントでサインアップ
2. 「**New +**」→「**Blueprint**」→ このリポジトリを選択（ブランチはこのコードがあるブランチを選ぶ）
3. 入力欄が出るので貼り付けて「Apply」
   - `DISCORD_TOKEN` … 手順1のトークン
   - `DATABASE_URL` … 手順2の接続文字列
   - `GUILD_ID` … テストサーバーのID（任意。入れるとコマンドがすぐ反映される）
4. Logs に `Logged in as ...` と出れば起動完了

以降は GitHub にプッシュするたびに自動で再デプロイされます。

### 4. サーバーで使い始める
フリマ用チャンネルで `/フリマ設置` を実行すれば準備完了です。

### ローカルPCで動かす場合（開発用）
```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # DISCORD_TOKEN を書き込む（DATABASE_URL 未設定なら SQLite に保存）
python bot.py
```

## 補足
- データは `DATABASE_URL` があれば PostgreSQL、なければ SQLite（`fleamarket.db`）に保存されます。Botを再起動しても、過去の出品ページやDMのボタンはそのまま使えます。
- Render の Logs に `429` や `1015` / `You are being rate limited` が出て起動しない場合は、Render の共有IPが Discord に一時的に制限されています。「Manual Deploy → Deploy latest commit」で再デプロイすると直ることが多いです。
- DMを受け取らない設定のメンバーには、出品チャンネルでメンションして通知します（取引ページURLもそこに表示されるため、**サーバーメンバーからのDMを許可**しておくのがおすすめです）。
- 同時に複数人が購入ボタンを押しても、先に確定した1人だけが取引に進みます。

## テスト
```bash
python -m unittest
# PostgreSQL でも試す場合
TEST_DATABASE_URL=postgresql://... python -m unittest
```
