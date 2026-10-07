// owner 已存在時，不用 owner 密碼取得 init 用的 API key（#1534）。
//
// 由 src/services/n8n_init.py 讀成字串後在租戶 n8n 容器內以 `node -e <source>` 執行。限制與
// reset_owner_password.js 相同（違反時 unit test 看不出來）：
// - 只能 require node 內建模組，以及「n8n 安裝目錄」解析得到的套件（sqlite3／pg／@n8n/permissions）
// - 不得依賴 __filename（node -e 沒有實體檔）
// - stdout 的最後一個非空行必須是 JSON（呼叫端只解析那一行）
//
// ⛔ 參數只從 stdin 讀（JSON），不進 argv；API key 只出現在最後一行輸出。
//
// 為什麼不再 `POST /rest/login`：owner 密碼是租戶的。租戶在 n8n 介面改了密碼或開了 MFA 之後，
// 平台存的那組就登不進去（401／998），以前 Redeploy 的 init 因此連續失敗、服務轉 error。
// init 在 owner 已存在時真正需要的只有「一把可用的 API key」，所以：
//   1. 找 owner（必須恰好一個、已完成 setup）；平台還沒確認過 owner（expected_email 有值）時，
//      owner 的 Email 必須是開通時設定的那一個——否則可能是別人搶先完成了 setup，不發 key。
//   2. owner 名下已有同名（label）的 key，且用它打公開 API 回 200 ⇒ 沿用。
//   3. 否則以 n8n 自己的方式簽一把新的 JWT key（HS256；iss=n8n、aud=public-api、sub=owner id，
//      與 PublicApiKeyService.generateApiKey 相同），刪掉同名舊 key 後寫入，再用公開 API 驗一次；
//      驗不過就把剛寫的那列刪掉、回報失敗——簽章密鑰的來源判斷錯了也不會留下壞 key。
//
// 依賴 n8n 內部 schema（user／user_api_keys／deployment_key 表、roleSlug 欄位、DB_TABLE_PREFIX）
// 與簽章密鑰的來源順序（N8N_USER_MANAGEMENT_JWT_SECRET → deployment_key 的 signing.jwt →
// encryptionKey 推導）。n8n 升版時要重跑 operator 的 integration test
// （tests/integration/test_n8n_init_owner_changed_1534.py）。
"use strict";

const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const path = require("path");
const { createRequire } = require("module");

const OWNER_ROLE = "global:owner";
const API_KEY_AUDIENCE = "public-api";
const API_KEY_ISSUER = "n8n";
const IDENTIFIER_RE = /^[A-Za-z0-9_]*$/;
const ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
// @n8n/permissions 載不到時的退路（與 n8n_init.py `_get_api_key_scopes` 的 fallback 相同）。
const FALLBACK_SCOPES = [
  "workflow:list", "workflow:read", "workflow:create", "workflow:update", "workflow:delete",
  "workflow:activate", "workflow:deactivate", "execution:read", "execution:list",
];

class EnsureFailure extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function report(payload) {
  process.stdout.write(JSON.stringify(payload) + "\n");
  if (!payload.success) process.exitCode = 1;
}

function n8nRequire() {
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir) continue;
    const candidate = path.join(dir, "n8n");
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return createRequire(fs.realpathSync(candidate));
    } catch (e) {
      // 不在這個目錄，繼續找
    }
  }
  return null;
}

function requireFromN8n(req, name) {
  try {
    return req(name);
  } catch (e) {
    throw new EnsureFailure("module_unavailable", `cannot load ${name} from the n8n installation`);
  }
}

// 與 n8n 的 @n8n/config 相同的讀法：環境變數優先，其次 <NAME>_FILE（不 trim）。
function envOrFile(name) {
  if (name in process.env) return process.env[name];
  const file = process.env[`${name}_FILE`];
  if (file) return fs.readFileSync(file, "utf8");
  return undefined;
}

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on("data", (c) => chunks.push(c));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", reject);
  });
}

function prefixed(name) {
  const prefix = process.env.DB_TABLE_PREFIX || "";
  if (!IDENTIFIER_RE.test(prefix)) {
    throw new EnsureFailure("db_unsupported", "DB_TABLE_PREFIX contains unsupported characters");
  }
  return `${prefix}${name}`;
}

function pickOwner(rows, expectedEmail) {
  if (rows.length === 0) throw new EnsureFailure("owner_missing", "no n8n owner account found");
  if (rows.length > 1) throw new EnsureFailure("owner_ambiguous", "more than one n8n owner account found");
  const owner = rows[0];
  if (!owner.email) throw new EnsureFailure("owner_missing", "the n8n owner account has not been set up");
  if (expectedEmail && String(owner.email).toLowerCase() !== String(expectedEmail).toLowerCase()) {
    // 不回傳 owner 的 Email：結果會進平台的 attempt 記錄。
    throw new EnsureFailure("owner_mismatch", "the n8n owner is not the account set at provisioning");
  }
  return owner;
}

function newId() {
  const bytes = crypto.randomBytes(16);
  let id = "";
  for (const b of bytes) id += ID_ALPHABET[b % ID_ALPHABET.length];
  return id;
}

function b64url(value) {
  return Buffer.from(typeof value === "string" ? value : JSON.stringify(value)).toString("base64url");
}

function signJwt(payload, secret) {
  const head = b64url({ alg: "HS256", typ: "JWT" });
  const body = b64url(payload);
  const sig = crypto.createHmac("sha256", secret).update(`${head}.${body}`).digest("base64url");
  return `${head}.${body}.${sig}`;
}

// n8n JwtService 的推導：encryptionKey 取偶數位字元再 sha256。
function derivedJwtSecret() {
  let key = envOrFile("N8N_ENCRYPTION_KEY");
  if (!key) {
    const home = process.env.N8N_USER_FOLDER || process.env.HOME || process.cwd();
    try {
      key = JSON.parse(fs.readFileSync(path.join(home, ".n8n", "config"), "utf8")).encryptionKey;
    } catch (e) {
      key = undefined;
    }
  }
  if (!key) return null;
  let base = "";
  for (let i = 0; i < key.length; i += 2) base += key[i];
  return crypto.createHash("sha256").update(base).digest("hex");
}

function apiCheck(apiKey) {
  // 用 n8n 自己的公開 API 驗 key（signature、audience、user 是否停用都由 n8n 判斷）。
  const port = Number(process.env.N8N_PORT || 5678);
  return new Promise((resolve) => {
    const req = http.request(
      { hostname: "localhost", port, path: "/api/v1/workflows?limit=1", method: "GET",
        headers: { "X-N8N-API-KEY": apiKey, Accept: "application/json" }, timeout: 8000 },
      (res) => { res.resume(); res.on("end", () => resolve(res.statusCode)); },
    );
    req.on("timeout", () => req.destroy(new Error("timeout")));
    req.on("error", () => resolve(0));
    req.end();
  });
}

// ---- DB 介面：sqlite（預設，PVC 上）／postgres -----------------------------------

function sqliteCall(db, method, sql, params) {
  return new Promise((resolve, reject) => {
    db[method](sql, params || [], function (err, result) {
      if (err) reject(err);
      else resolve(method === "run" ? this.changes : result);
    });
  });
}

async function openSqlite(req) {
  const sqlite3 = requireFromN8n(req, "sqlite3");
  const home = process.env.N8N_USER_FOLDER || process.env.HOME || process.cwd();
  const file = path.resolve(path.join(home, ".n8n"), process.env.DB_SQLITE_DATABASE || "database.sqlite");
  if (!fs.existsSync(file)) throw new EnsureFailure("owner_missing", "n8n database file not found");
  const db = await new Promise((resolve, reject) => {
    const handle = new sqlite3.Database(file, sqlite3.OPEN_READWRITE, (err) => (err ? reject(err) : resolve(handle)));
  });
  db.configure("busyTimeout", 10000);
  const q = (name) => `"${prefixed(name)}"`;
  return {
    q,
    all: (sql, params) => sqliteCall(db, "all", sql, params),
    run: (sql, params) => sqliteCall(db, "run", sql, params),
    begin: () => sqliteCall(db, "run", "BEGIN IMMEDIATE"),
    commit: () => sqliteCall(db, "run", "COMMIT"),
    rollback: () => sqliteCall(db, "run", "ROLLBACK").catch(() => {}),
    columns: async (name) => (await sqliteCall(db, "all", `PRAGMA table_info(${q(name)})`)).map((c) => c.name),
    hasTable: async (name) =>
      (await sqliteCall(db, "all", "SELECT name FROM sqlite_master WHERE type='table' AND name=?", [prefixed(name)])).length > 0,
    placeholder: () => "?",
    close: () => new Promise((resolve) => db.close(() => resolve())),
  };
}

async function openPostgres(req) {
  const pg = requireFromN8n(req, "pg");
  const schema = envOrFile("DB_POSTGRESDB_SCHEMA") || "public";
  if (!IDENTIFIER_RE.test(schema)) throw new EnsureFailure("db_unsupported", "DB_POSTGRESDB_SCHEMA contains unsupported characters");
  const sslEnabled = envOrFile("DB_POSTGRESDB_SSL_ENABLED") === "true";
  const client = new pg.Client({
    host: envOrFile("DB_POSTGRESDB_HOST") || "localhost",
    port: Number(envOrFile("DB_POSTGRESDB_PORT") || 5432),
    database: envOrFile("DB_POSTGRESDB_DATABASE") || "n8n",
    user: envOrFile("DB_POSTGRESDB_USER") || "postgres",
    password: envOrFile("DB_POSTGRESDB_PASSWORD") || "",
    ssl: sslEnabled ? { rejectUnauthorized: envOrFile("DB_POSTGRESDB_SSL_REJECT_UNAUTHORIZED") !== "false" } : false,
    connectionTimeoutMillis: 10000,
  });
  await client.connect();
  let n = 0;
  const q = (name) => `"${schema}"."${prefixed(name)}"`;
  // sqlite 風格的 ? 轉成 $1、$2……（本檔的 SQL 字串裡沒有其他問號）。
  const conv = (sql) => { n = 0; return sql.replace(/\?/g, () => `$${++n}`); };
  return {
    q,
    all: async (sql, params) => (await client.query(conv(sql), params || [])).rows,
    run: async (sql, params) => (await client.query(conv(sql), params || [])).rowCount,
    begin: () => client.query("BEGIN"),
    commit: () => client.query("COMMIT"),
    rollback: () => client.query("ROLLBACK").catch(() => {}),
    columns: async (name) => (await client.query(
      "SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2",
      [schema, prefixed(name)])).rows.map((r) => r.column_name),
    hasTable: async (name) => (await client.query(
      "SELECT 1 FROM information_schema.tables WHERE table_schema = $1 AND table_name = $2",
      [schema, prefixed(name)])).rowCount > 0,
    close: () => client.end().catch(() => {}),
  };
}

async function jwtSecret(db) {
  // 順序與 n8n 2.x JwtService 相同：環境變數 → DB 的 active signing.jwt → encryptionKey 推導。
  const fromEnv = envOrFile("N8N_USER_MANAGEMENT_JWT_SECRET");
  if (fromEnv) return fromEnv;
  if (await db.hasTable("deployment_key")) {
    const rows = await db.all(
      `SELECT "value" FROM ${db.q("deployment_key")} WHERE "type" = ? AND "status" = ?`, ["signing.jwt", "active"]);
    if (rows.length === 1 && rows[0].value) return rows[0].value;
  }
  const derived = derivedJwtSecret();
  if (derived) return derived;
  throw new EnsureFailure("signing_secret_unavailable", "cannot determine the n8n JWT signing secret");
}

function ownerScopes(req) {
  try {
    const scopes = req("@n8n/permissions").OWNER_API_KEY_SCOPES;
    if (Array.isArray(scopes) && scopes.length) return scopes;
  } catch (e) {
    // 退回固定清單
  }
  return FALLBACK_SCOPES;
}

async function main() {
  let input;
  try {
    input = JSON.parse(await readStdin());
  } catch (e) {
    throw new EnsureFailure("invalid_input", "stdin is not a JSON object");
  }
  const label = input && input.label;
  if (typeof label !== "string" || !label || label.length > 100) {
    throw new EnsureFailure("invalid_input", "label is required");
  }
  const expectedEmail = input.expected_email || null;
  const expiresAt = Number(input.expires_at) || null;

  const req = n8nRequire();
  if (!req) throw new EnsureFailure("module_unavailable", "n8n installation not found on PATH");
  const dbType = envOrFile("DB_TYPE") || "sqlite";
  let db;
  if (dbType === "sqlite") db = await openSqlite(req);
  else if (dbType === "postgresdb") db = await openPostgres(req);
  else throw new EnsureFailure("db_unsupported", `DB_TYPE ${dbType} is not supported`);

  try {
    const userCols = await db.columns("user");
    const roleColumn = userCols.includes("roleSlug") ? "roleSlug" : userCols.includes("role") ? "role" : null;
    if (!roleColumn) throw new EnsureFailure("db_unsupported", "n8n user table has no role column");
    const owner = pickOwner(
      await db.all(`SELECT "id", "email" FROM ${db.q("user")} WHERE "${roleColumn}" = ?`, [OWNER_ROLE]),
      expectedEmail);
    const keys = db.q("user_api_keys");

    // 2. 沿用：owner 名下同名的 key 仍可用就不換（不必每次 Redeploy 都換 key）。
    const existing = await db.all(
      `SELECT "apiKey" FROM ${keys} WHERE "userId" = ? AND "label" = ? AND "audience" = ?`,
      [owner.id, label, API_KEY_AUDIENCE]);
    for (const row of existing) {
      if (row.apiKey && (await apiCheck(row.apiKey)) === 200) {
        return { api_key: row.apiKey, reused: true };
      }
    }

    // 3. 新簽一把；同名舊 key（已失效）先刪，(userId, label) 有唯一限制。
    const secret = await jwtSecret(db);
    const now = Math.floor(Date.now() / 1000);
    const payload = { sub: owner.id, iss: API_KEY_ISSUER, aud: API_KEY_AUDIENCE, jti: crypto.randomUUID(), iat: now };
    if (expiresAt && expiresAt > now) payload.exp = expiresAt;
    const apiKey = signJwt(payload, secret);
    const id = newId();
    await db.begin();
    try {
      await db.run(`DELETE FROM ${keys} WHERE "userId" = ? AND "label" = ?`, [owner.id, label]);
      await db.run(
        `INSERT INTO ${keys} ("id", "userId", "label", "apiKey", "scopes", "audience") VALUES (?, ?, ?, ?, ?, ?)`,
        [id, owner.id, label, apiKey, JSON.stringify(ownerScopes(req)), API_KEY_AUDIENCE]);
      await db.commit();
    } catch (e) {
      await db.rollback();
      throw e;
    }
    const status = await apiCheck(apiKey);
    if (status !== 200) {
      await db.run(`DELETE FROM ${keys} WHERE "id" = ?`, [id]).catch(() => {});
      throw new EnsureFailure("key_unverified", `n8n rejected the new API key (HTTP ${status})`);
    }
    return { api_key: apiKey, reused: false };
  } finally {
    await db.close();
  }
}

main()
  .then((result) => report({ success: true, api_key: result.api_key, reused: result.reused }))
  .catch((e) => {
    if (e instanceof EnsureFailure) report({ success: false, code: e.code, error: e.message });
    // 非預期例外只回報類別：DB driver 的訊息可能帶連線字串，不往外送。
    else report({ success: false, error: `${(e && e.name) || "Error"} while ensuring the API key` });
  });
