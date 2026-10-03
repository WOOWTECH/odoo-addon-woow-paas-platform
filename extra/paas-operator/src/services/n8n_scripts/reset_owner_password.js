// 重設租戶 n8n 的 owner 密碼，並關掉 owner 的 MFA（#1370）。
//
// 由 src/services/app_admin_password.py 讀成字串後在租戶 n8n 容器內以
// `node -e <source>` 執行。限制（與 odoo_scripts/ 同一套，違反時 unit test 看不出來）：
// - 只能 require node 內建模組，以及「n8n 安裝目錄」解析得到的套件（bcryptjs／sqlite3／pg）
// - 不得依賴 __filename（node -e 沒有實體檔）
// - stdout 的最後一個非空行必須是 JSON（呼叫端只解析那一行）
//
// ⛔ 新密碼只從 stdin 讀（operator 以 kubectl exec --stdin 送入）：不進 argv、不進輸出。
//
// 為什麼不用 `n8n user-management:reset`：它把使用者管理打回 setup 模式——服務網址是
// 公開的，在租戶重新設定 owner 之前任何人都能搶先接手所有 workflow 與 credential。
// 這裡改成直接更新 owner 那一列的 password（與 n8n 自己的 PasswordUtility 相同：
// bcryptjs、cost 10），MFA 欄位清成與 `n8n mfa:disable` 相同的值
// （mfaSecret=NULL、mfaRecoveryCodes=''〔simple-array 的空陣列〕、mfaEnabled=false）。
//
// 依賴 n8n 的內部 schema（user 表、roleSlug／role 欄位、DB_TABLE_PREFIX）。因此
// owner 必須「恰好一列」才寫，否則整筆 rollback——寧可失敗也不可改錯人。n8n 升版時
// 要重跑 operator 的 integration test（tests/integration/test_app_admin_password_reset.py）。
"use strict";

const fs = require("fs");
const path = require("path");
const { createRequire } = require("module");

const OWNER_ROLE = "global:owner";
const BCRYPT_ROUNDS = 10;
const MIN_PASSWORD_LENGTH = 12;
const MAX_PASSWORD_LENGTH = 128;
const IDENTIFIER_RE = /^[A-Za-z0-9_]*$/;

class ResetFailure extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function report(payload) {
  process.stdout.write(JSON.stringify(payload) + "\n");
  if (!payload.success) process.exitCode = 1;
}

// 從 PATH 找 n8n 的啟動檔，以它的真實位置建立 require——不寫死安裝目錄或版本資料夾。
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
    throw new ResetFailure("hash_unavailable", `cannot load ${name} from the n8n installation`);
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

function stripOneNewline(value) {
  if (value.endsWith("\n")) value = value.slice(0, -1);
  if (value.endsWith("\r")) value = value.slice(0, -1);
  return value;
}

function tableName() {
  const prefix = process.env.DB_TABLE_PREFIX || "";
  if (!IDENTIFIER_RE.test(prefix)) {
    throw new ResetFailure("db_unsupported", "DB_TABLE_PREFIX contains unsupported characters");
  }
  return `${prefix}user`;
}

function pickOwner(rows) {
  if (rows.length === 0) throw new ResetFailure("admin_missing", "no n8n owner account found");
  if (rows.length > 1) throw new ResetFailure("owner_ambiguous", "more than one n8n owner account found");
  const owner = rows[0];
  if (!owner.email) {
    // owner 空殼＝實例還在 setup 模式（從沒完成 owner setup）；設密碼救不了它。
    throw new ResetFailure("admin_missing", "the n8n owner account has not been set up");
  }
  return owner;
}

// ---- SQLite（n8n 預設；檔案在 PVC 上）------------------------------------

function sqliteCall(db, method, sql, params) {
  return new Promise((resolve, reject) => {
    db[method](sql, params || [], function (err, result) {
      if (err) reject(err);
      else resolve(method === "run" ? this.changes : result);
    });
  });
}

async function resetSqlite(req, hash) {
  const sqlite3 = requireFromN8n(req, "sqlite3");
  const home = process.env.N8N_USER_FOLDER || process.env.HOME || process.cwd();
  const file = path.resolve(path.join(home, ".n8n"), process.env.DB_SQLITE_DATABASE || "database.sqlite");
  if (!fs.existsSync(file)) throw new ResetFailure("admin_missing", "n8n database file not found");

  const db = await new Promise((resolve, reject) => {
    const handle = new sqlite3.Database(file, sqlite3.OPEN_READWRITE, (err) => (err ? reject(err) : resolve(handle)));
  });
  db.configure("busyTimeout", 10000);
  const table = tableName();
  try {
    await sqliteCall(db, "run", "BEGIN IMMEDIATE");
    try {
      const columns = (await sqliteCall(db, "all", `PRAGMA table_info("${table}")`)).map((c) => c.name);
      const roleColumn = columns.includes("roleSlug") ? "roleSlug" : columns.includes("role") ? "role" : null;
      if (!roleColumn) throw new ResetFailure("db_unsupported", "n8n user table has no role column");
      const rows = await sqliteCall(
        db, "all",
        `SELECT "id", "email", "mfaEnabled" FROM "${table}" WHERE "${roleColumn}" = ?`, [OWNER_ROLE],
      );
      const owner = pickOwner(rows);
      const changed = await sqliteCall(
        db, "run",
        `UPDATE "${table}" SET "password" = ?, "mfaEnabled" = 0, "mfaSecret" = NULL, ` +
          `"mfaRecoveryCodes" = '' WHERE "id" = ? AND "${roleColumn}" = ?`,
        [hash, owner.id, OWNER_ROLE],
      );
      if (changed !== 1) throw new ResetFailure("owner_ambiguous", `owner update touched ${changed} rows`);
      await sqliteCall(db, "run", "COMMIT");
      return { login: owner.email, secondFactorCleared: Boolean(owner.mfaEnabled) };
    } catch (e) {
      await sqliteCall(db, "run", "ROLLBACK").catch(() => {});
      throw e;
    }
  } finally {
    await new Promise((resolve) => db.close(() => resolve()));
  }
}

// ---- PostgreSQL（開通表單可選 postgres）-----------------------------------

async function resetPostgres(req, hash) {
  const pg = requireFromN8n(req, "pg");
  const schema = envOrFile("DB_POSTGRESDB_SCHEMA") || "public";
  if (!IDENTIFIER_RE.test(schema)) throw new ResetFailure("db_unsupported", "DB_POSTGRESDB_SCHEMA contains unsupported characters");
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
  const table = tableName();
  await client.connect();
  try {
    await client.query("BEGIN");
    try {
      const cols = await client.query(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2",
        [schema, table],
      );
      const columns = cols.rows.map((r) => r.column_name);
      const roleColumn = columns.includes("roleSlug") ? "roleSlug" : columns.includes("role") ? "role" : null;
      if (!roleColumn) throw new ResetFailure("db_unsupported", "n8n user table has no role column");
      const qualified = `"${schema}"."${table}"`;
      const res = await client.query(
        `SELECT "id", "email", "mfaEnabled" FROM ${qualified} WHERE "${roleColumn}" = $1 FOR UPDATE`, [OWNER_ROLE],
      );
      const owner = pickOwner(res.rows);
      const upd = await client.query(
        `UPDATE ${qualified} SET "password" = $1, "mfaEnabled" = false, "mfaSecret" = NULL, ` +
          `"mfaRecoveryCodes" = '' WHERE "id" = $2 AND "${roleColumn}" = $3`,
        [hash, owner.id, OWNER_ROLE],
      );
      if (upd.rowCount !== 1) throw new ResetFailure("owner_ambiguous", `owner update touched ${upd.rowCount} rows`);
      await client.query("COMMIT");
      return { login: owner.email, secondFactorCleared: Boolean(owner.mfaEnabled) };
    } catch (e) {
      await client.query("ROLLBACK").catch(() => {});
      throw e;
    }
  } finally {
    await client.end().catch(() => {});
  }
}

async function main() {
  const password = stripOneNewline(await readStdin());
  if (password.length < MIN_PASSWORD_LENGTH || password.length > MAX_PASSWORD_LENGTH) {
    throw new ResetFailure("invalid_password", "new password length out of range");
  }
  const req = n8nRequire();
  if (!req) throw new ResetFailure("hash_unavailable", "n8n installation not found on PATH");
  const bcrypt = requireFromN8n(req, "bcryptjs");
  const hash = await bcrypt.hash(password, BCRYPT_ROUNDS);

  const dbType = envOrFile("DB_TYPE") || "sqlite";
  let result;
  if (dbType === "sqlite") result = await resetSqlite(req, hash);
  else if (dbType === "postgresdb") result = await resetPostgres(req, hash);
  else throw new ResetFailure("db_unsupported", `DB_TYPE ${dbType} is not supported`);
  report({ success: true, login: result.login, second_factor_cleared: result.secondFactorCleared });
}

main().catch((e) => {
  if (e instanceof ResetFailure) report({ success: false, code: e.code, error: e.message });
  // 非預期例外只回報類別：DB driver 的訊息可能帶連線字串，不往外送。
  else report({ success: false, error: `${(e && e.name) || "Error"} while resetting` });
});
