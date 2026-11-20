///usr/bin/env jbang "$0" "$@" ; exit $?
//JAVA 17+
//DEPS com.github.mwiede:jsch:0.2.20

// db2q - minimal read-only SQL CLI for a coding agent.
// Opens its own SSH tunnel (optional), connects through any JDBC driver,
// runs one read-only query and prints tab-separated rows.
//
// CONFIGURATION
//   Everything comes from a KEY=VALUE env file, then process environment
//   variables, then --set KEY=VALUE arguments (later wins).
//   Env file lookup: --env FILE, else $DB2Q_ENV, else ./db2q.env,
//   else ~/.db2q/db2q.env. See db2q.env.example for every key.
//
// USAGE
//   db2q "SELECT ..."               run a query (SELECT / WITH / VALUES only)
//   echo "SELECT ..." | db2q -      read SQL from stdin
//   db2q --check                    test the connection, print server/user/schema
//   db2q --tables [SCHEMA] [PATTERN]
//   db2q --describe [SCHEMA.]TABLE
//   options: --env FILE   --set KEY=VALUE (repeatable)   --max N

import com.jcraft.jsch.*;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.sql.*;
import java.util.*;

public class db2q {

    static final Map<String, String> CFG = new LinkedHashMap<>();
    static final String DEFAULT_URL_TEMPLATE = "jdbc:datadirect:db2://{host}:{port};DatabaseName={database}";
    static final String DEFAULT_DRIVER = "com.ddtek.jdbc.db2.DB2Driver";

    public static void main(String[] argv) throws Exception {
        List<String> args = new ArrayList<>();
        String envFile = null;
        Map<String, String> overrides = new LinkedHashMap<>();
        Integer maxOverride = null;
        for (int i = 0; i < argv.length; i++) {
            switch (argv[i]) {
                case "--env" -> envFile = next(argv, ++i, "--env FILE");
                case "--set" -> {
                    String kv = next(argv, ++i, "--set KEY=VALUE");
                    int eq = kv.indexOf('=');
                    if (eq <= 0) die("--set expects KEY=VALUE");
                    overrides.put(kv.substring(0, eq).trim(), kv.substring(eq + 1));
                }
                case "--max" -> maxOverride = Integer.parseInt(next(argv, ++i, "--max N"));
                default -> args.add(argv[i]);
            }
        }
        loadConfig(envFile, overrides);
        if (args.isEmpty()) usage();

        int maxRows = maxOverride != null ? maxOverride : Integer.parseInt(cfg("DB2Q_MAX_ROWS", "200"));
        String mode = args.get(0);

        String sql = null;
        if (!mode.startsWith("--")) {
            sql = mode.equals("-")
                    ? new String(System.in.readAllBytes(), StandardCharsets.UTF_8)
                    : String.join(" ", args);
            sql = sql.trim().replaceAll(";\\s*$", "");
            guard(sql); // refuse before opening anything
        } else if (!Set.of("--check", "--tables", "--describe").contains(mode)) {
            usage();
        }

        Session ssh = null;
        try {
            String host = req("DB2Q_DB_HOST");
            int port = Integer.parseInt(req("DB2Q_DB_PORT"));
            if (!cfg("DB2Q_SSH_HOST", "").isBlank()) {
                ssh = openTunnel(host, port);
                port = tunnelPort;
                host = "127.0.0.1";
            }
            String url = cfg("DB2Q_URL_TEMPLATE", DEFAULT_URL_TEMPLATE)
                    .replace("{host}", host)
                    .replace("{port}", String.valueOf(port))
                    .replace("{database}", cfg("DB2Q_DB_NAME", ""));

            try (Connection c = connect(url)) {
                c.setReadOnly(true);
                c.setAutoCommit(true);
                switch (mode) {
                    case "--check" -> {
                        try (Statement s = c.createStatement()) {
                            print(s.executeQuery(cfg("DB2Q_CHECK_SQL",
                                    "SELECT CURRENT SERVER AS SERVER, CURRENT SQLID AS SQLID, CURRENT SCHEMA AS SCHEMA FROM SYSIBM.SYSDUMMY1")),
                                    null, 10);
                        }
                        System.err.println("db2q: OK" + (ssh != null ? " (via SSH tunnel)" : ""));
                    }
                    case "--tables" -> {
                        String schema = args.size() > 1 ? args.get(1).toUpperCase() : defaultSchema();
                        String pattern = args.size() > 2 ? args.get(2).toUpperCase() : "%";
                        print(c.getMetaData().getTables(null, schema, pattern, new String[]{"TABLE", "VIEW", "ALIAS"}),
                              Set.of("TABLE_SCHEM", "TABLE_NAME", "TABLE_TYPE", "REMARKS"), maxRows);
                    }
                    case "--describe" -> {
                        if (args.size() < 2) die("--describe needs [SCHEMA.]TABLE");
                        String t = args.get(1).toUpperCase();
                        String schema = t.contains(".") ? t.substring(0, t.indexOf('.')) : defaultSchema();
                        String table = t.contains(".") ? t.substring(t.indexOf('.') + 1) : t;
                        if (schema == null) die("no schema given and DB2Q_DEFAULT_SCHEMA not set");
                        print(c.getMetaData().getColumns(null, schema, table, "%"),
                              Set.of("COLUMN_NAME", "TYPE_NAME", "COLUMN_SIZE", "DECIMAL_DIGITS", "IS_NULLABLE", "REMARKS"),
                              10_000);
                    }
                    default -> {
                        try (Statement s = c.createStatement()) {
                            s.setMaxRows(maxRows + 1); // +1 to detect truncation
                            s.setQueryTimeout(Integer.parseInt(cfg("DB2Q_TIMEOUT", "60")));
                            print(s.executeQuery(sql), null, maxRows);
                        }
                    }
                }
            }
        } catch (SQLException e) {
            die("SQL error " + e.getErrorCode() + " / SQLSTATE " + e.getSQLState() + ": " + e.getMessage());
        } catch (JSchException e) {
            die("SSH tunnel failed: " + e.getMessage());
        } catch (IOException | ReflectiveOperationException e) {
            die("cannot load driver: " + e);
        } finally {
            if (ssh != null) ssh.disconnect();
        }
    }

    // ---------------------------------------------------------------- config

    static void loadConfig(String explicit, Map<String, String> overrides) throws IOException {
        Path file = null;
        if (explicit != null) {
            file = expand(explicit);
            if (!Files.isRegularFile(file)) die("env file not found: " + file);
        } else if (System.getenv("DB2Q_ENV") != null) {
            file = expand(System.getenv("DB2Q_ENV"));
            if (!Files.isRegularFile(file)) die("DB2Q_ENV points to a missing file: " + file);
        } else {
            for (Path p : List.of(Paths.get("db2q.env"), expand("~/.db2q/db2q.env"))) {
                if (Files.isRegularFile(p)) { file = p; break; }
            }
        }
        if (file != null) {
            for (String line : Files.readAllLines(file, StandardCharsets.UTF_8)) {
                String l = line.strip();
                if (l.isEmpty() || l.startsWith("#")) continue;
                if (l.startsWith("export ")) l = l.substring(7).strip();
                int eq = l.indexOf('=');
                if (eq <= 0) continue;
                String k = l.substring(0, eq).strip();
                String v = l.substring(eq + 1).strip();
                if (v.length() >= 2 && (v.startsWith("\"") && v.endsWith("\"") || v.startsWith("'") && v.endsWith("'"))) {
                    v = v.substring(1, v.length() - 1);
                }
                CFG.put(k, v);
            }
        }
        System.getenv().forEach((k, v) -> { if (k.startsWith("DB2Q_") && !k.equals("DB2Q_ENV")) CFG.put(k, v); });
        CFG.putAll(overrides);
    }

    static String defaultSchema() {
        String s = cfg("DB2Q_DEFAULT_SCHEMA", "");
        return s.isBlank() ? null : s.toUpperCase();
    }

    // ------------------------------------------------------------------- ssh

    static Session openTunnel(String dbHost, int dbPort) throws JSchException {
        JSch jsch = new JSch();
        String key = cfg("DB2Q_SSH_KEY", "");
        if (!key.isBlank()) jsch.addIdentity(expand(key).toString());
        Path knownHosts = expand(cfg("DB2Q_SSH_KNOWN_HOSTS", "~/.ssh/known_hosts"));
        if (Files.isRegularFile(knownHosts)) jsch.setKnownHosts(knownHosts.toString());

        Session s = jsch.getSession(req("DB2Q_SSH_USER"), req("DB2Q_SSH_HOST"),
                Integer.parseInt(cfg("DB2Q_SSH_PORT", "22")));
        String pw = cfg("DB2Q_SSH_PASSWORD", "");
        if (!pw.isBlank()) {
            s.setPassword(pw);
            s.setUserInfo(new PasswordInfo(pw)); // covers keyboard-interactive too
        }
        s.setConfig("StrictHostKeyChecking", cfg("DB2Q_SSH_STRICT", "no"));
        s.setConfig("PreferredAuthentications", "publickey,keyboard-interactive,password");
        s.setServerAliveInterval(15_000);
        s.connect(Integer.parseInt(cfg("DB2Q_SSH_TIMEOUT_MS", "15000")));
        tunnelPort = s.setPortForwardingL("127.0.0.1", 0, dbHost, dbPort); // 0 = any free local port
        return s;
    }

    static int tunnelPort;

    static final class PasswordInfo implements UserInfo, UIKeyboardInteractive {
        private final String pw;
        PasswordInfo(String pw) { this.pw = pw; }
        public String getPassword() { return pw; }
        public boolean promptPassword(String m) { return true; }
        public String getPassphrase() { return null; }
        public boolean promptPassphrase(String m) { return false; }
        public boolean promptYesNo(String m) { return false; }
        public void showMessage(String m) { }
        public String[] promptKeyboardInteractive(String dest, String name, String instr, String[] prompt, boolean[] echo) {
            String[] r = new String[prompt.length];
            Arrays.fill(r, pw);
            return r;
        }
    }

    // ------------------------------------------------------------------ jdbc

    static Connection connect(String url) throws Exception {
        Path jar = expand(req("DB2Q_DRIVER_JAR"));
        if (!Files.isRegularFile(jar)) die("driver jar not found: " + jar);
        String cls = cfg("DB2Q_DRIVER", DEFAULT_DRIVER);
        URLClassLoader cl = new URLClassLoader(new URL[]{jar.toUri().toURL()}, db2q.class.getClassLoader());
        Driver d = (Driver) Class.forName(cls, true, cl).getDeclaredConstructor().newInstance();
        Properties p = new Properties();
        p.setProperty("user", req("DB2Q_USER"));
        p.setProperty("password", req("DB2Q_PASSWORD"));
        CFG.forEach((k, v) -> { if (k.startsWith("DB2Q_PROP_")) p.setProperty(k.substring(10), v); });
        Connection c = d.connect(url, p);
        if (c == null) die("driver " + cls + " does not accept URL " + url);
        return c;
    }

    // Belt and braces only: the real protection is a SELECT-only DB user.
    static void guard(String sql) {
        String s = sql.replaceAll("(?s)/\\*.*?\\*/", " ").replaceAll("--[^\\n]*", " ").trim().toUpperCase();
        if (!(s.startsWith("SELECT") || s.startsWith("WITH") || s.startsWith("VALUES"))) {
            die("refused: only SELECT / WITH / VALUES statements are allowed");
        }
        if (s.contains(";")) die("refused: one statement at a time");
        if (s.matches("(?s).*\\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|GRANT|REVOKE|TRUNCATE|CALL|LOCK)\\b.*")) {
            die("refused: statement contains a write/DDL keyword");
        }
    }

    // Tab-separated output: compact for the LLM, easy to read.
    static void print(ResultSet rs, Set<String> keep, int maxRows) throws SQLException {
        ResultSetMetaData md = rs.getMetaData();
        List<Integer> cols = new ArrayList<>();
        for (int i = 1; i <= md.getColumnCount(); i++) {
            if (keep == null || keep.contains(md.getColumnLabel(i).toUpperCase())) cols.add(i);
        }
        StringBuilder h = new StringBuilder();
        for (int i : cols) h.append(h.length() > 0 ? "\t" : "").append(md.getColumnLabel(i));
        System.out.println(h);
        int n = 0;
        boolean truncated = false;
        while (rs.next()) {
            if (n >= maxRows) { truncated = true; break; }
            StringBuilder r = new StringBuilder();
            for (int k = 0; k < cols.size(); k++) {
                Object v = rs.getObject(cols.get(k));
                String s = v == null ? "NULL" : v.toString().replace('\t', ' ').replace('\n', ' ').stripTrailing();
                r.append(k > 0 ? "\t" : "").append(s);
            }
            System.out.println(r);
            n++;
        }
        System.out.println("-- " + n + " row(s)" + (truncated ? " (TRUNCATED at " + maxRows + ", refine the query or use --max)" : ""));
    }

    // ----------------------------------------------------------------- utils

    static Path expand(String p) {
        return p.startsWith("~") ? Paths.get(System.getProperty("user.home") + p.substring(1)) : Paths.get(p);
    }
    static String cfg(String k, String def) { String v = CFG.get(k); return v == null || v.isBlank() ? def : v; }
    static String req(String k) { String v = CFG.get(k); if (v == null || v.isBlank()) die("missing config " + k); return v; }
    static String next(String[] a, int i, String what) { if (i >= a.length) die("missing value for " + what); return a[i]; }
    static void usage() {
        die("usage: db2q [--env FILE] [--set KEY=VALUE]... [--max N] "
                + "( \"SELECT ...\" | - | --check | --tables [SCHEMA] [PATTERN] | --describe [SCHEMA.]TABLE )");
    }
    static void die(String m) { System.err.println("db2q: " + m); System.exit(1); }
}
