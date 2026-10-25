"""Integration tests against real SQL Server instances (2017 and 2022), with both drivers."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from sql_mcp import server, session
from sql_mcp.session import SessionManager

from .conftest import TEST_DB, call, make_profile

pytestmark = pytest.mark.integration
T = server  # tool namespace


def rows(res, i=0):
    return res["result_sets"][i]["rows"]


def err(res):
    assert res["success"] is False, res
    return res["errors"][0]


# ====================================================================== connection

def test_test_connection_reports_version(ctx):
    res = call(T.test_connection)
    assert res["success"], res
    info = res["server"]
    expected = "SQL Server 2017" if ctx["target"][0].endswith("2017") else "SQL Server 2022"
    assert info["product"] == expected
    assert info["login_name"] == "sa" and info["is_sysadmin"] == 1
    assert info["auth_scheme"] == "SQL"
    assert info["sql_agent"] == "Running"
    assert ctx["driver"].split()[0] in info["driver"]


def test_list_connections_hides_password(ctx):
    res = call(T.list_connections)
    text = json.dumps(res)
    assert res["default"] == "main" and "password" not in text.replace("has_password", "")
    assert res["connections"][0]["connected"] is True


def test_unknown_connection_name(ctx):
    e = err(call(T.execute_sql, sql="SELECT 1", connection="nope"))
    assert "Unknown connection" in e["message"] and "main" in e["message"]


def test_wrong_password_is_reported(ctx):
    mgr = ctx["mgr"]
    mgr.add(make_profile(ctx["target"], ctx["driver"], name="badpw", password="wrong-password"))
    try:
        e = err(call(T.test_connection, connection="badpw"))
        assert "Login failed" in e["message"]
    finally:
        mgr.remove("badpw")


def test_unreachable_server_fails_fast(ctx):
    mgr = ctx["mgr"]
    mgr.add(make_profile(("x", "127.0.0.1", 1), ctx["driver"], name="dead", login_timeout=3))
    try:
        started = time.monotonic()
        e = err(call(T.test_connection, connection="dead"))
        assert time.monotonic() - started < 30
        assert e["message"]
    finally:
        mgr.remove("dead")


def test_add_connection_runtime_and_remove(ctx):
    _, host, port = ctx["target"]
    res = call(T.add_connection, name="rt", server=host, port=port, auth="sql", user="sa",
               password=make_profile(ctx["target"], ctx["driver"]).password, driver=ctx["driver"],
               database=TEST_DB)
    assert res["success"] and res["server"]["current_database"] == TEST_DB, res
    res = call(T.add_connection, name="rt2", server=host, auth="sql", user="sa")
    assert err(res)["message"].endswith("requires 'password' (or 'password_env')")
    assert call(T.remove_connection, name="rt")["success"]
    assert "Unknown connection" in err(call(T.remove_connection, name="rt"))["message"]


def test_no_configuration_gives_helpful_error(ctx):
    server.set_manager(SessionManager({}, None))
    try:
        e = err(call(T.execute_sql, sql="SELECT 1"))
        assert "SQL_MCP_SERVER" in e["message"]
    finally:
        server.set_manager(ctx["mgr"])


# ====================================================================== CRUD

def test_crud_round_trip(sql):
    call(T.test_connection)  # internal tool queries must not change session settings (NOCOUNT)
    call(T.list_jobs)
    sql("CREATE TABLE dbo.Customer (Id int IDENTITY PRIMARY KEY, Name nvarchar(100) NOT NULL, "
        "City nvarchar(50) NULL, Balance decimal(18,2) NOT NULL DEFAULT 0)")
    res = sql("INSERT INTO dbo.Customer (Name, City, Balance) VALUES (?, ?, ?)", params=["Ahmet", "İstanbul", 10.5])
    assert res["rows_affected"] == [1]
    res = sql("INSERT INTO dbo.Customer (Name, City) VALUES (N'Ayşe',N'Ankara'),(N'Çağrı',N'İzmir'),(N'Öykü',NULL)")
    assert res["rows_affected"] == [3]
    res = sql("SELECT Id, Name, City, Balance FROM dbo.Customer ORDER BY Id")
    assert res["result_sets"][0]["columns"] == ["Id", "Name", "City", "Balance"]
    assert rows(res) == [[1, "Ahmet", "İstanbul", "10.50"], [2, "Ayşe", "Ankara", 0],
                         [3, "Çağrı", "İzmir", 0], [4, "Öykü", None, 0]]
    res = sql("UPDATE dbo.Customer SET Balance = Balance + 1 WHERE City IS NOT NULL")
    assert res["rows_affected"] == [3]
    res = sql("DELETE FROM dbo.Customer WHERE Id = ?", params=[4])
    assert res["rows_affected"] == [1]
    res = sql("UPDATE dbo.Customer SET Name = Name WHERE 1 = 0")
    assert res["rows_affected"] == [0]
    res = sql("SELECT COUNT(*) AS n FROM dbo.Customer WHERE Name LIKE ?", params=["%ç%"])
    assert rows(res) == [[1]]


def test_merge_and_output_clause(sql):
    sql("CREATE TABLE dbo.M (k int PRIMARY KEY, v nvarchar(10))")
    sql("INSERT dbo.M VALUES (1,'a'),(2,'b')")
    res = sql("""MERGE dbo.M AS t USING (VALUES (2,'B'),(3,'C')) AS s(k,v) ON t.k = s.k
                 WHEN MATCHED THEN UPDATE SET v = s.v
                 WHEN NOT MATCHED THEN INSERT (k, v) VALUES (s.k, s.v)
                 OUTPUT $action AS act, inserted.k;""")
    assert sorted(rows(res)) == [["INSERT", 3], ["UPDATE", 2]]


def test_data_types(sql):
    res = sql("""SELECT CAST(12345678901234 AS bigint) AS big, CAST(1.25 AS float) AS f, CAST(2.5 AS real) AS r,
        CAST(123.4567 AS money) AS m, CAST(99999999999999999.99 AS decimal(19,2)) AS d,
        CAST(1 AS bit) AS b, CAST(255 AS tinyint) AS ti,
        CAST('2024-02-29 13:14:15.1234567' AS datetime2(7)) AS dt2,
        CAST('2024-02-29T13:14:15.123+03:00' AS datetimeoffset(3)) AS dto,
        CAST('2024-02-29' AS date) AS dd, CAST('23:59:59.5' AS time(1)) AS tt,
        CAST('2024-02-29 10:00' AS smalldatetime) AS sdt,
        CAST('6F9619FF-8B86-D011-B42D-00C04FC964FF' AS uniqueidentifier) AS g,
        CAST(0x00FF10 AS varbinary(10)) AS vb, CAST(NULL AS int) AS n,
        CAST(N'<a x="1">ş</a>' AS xml) AS x, N'ğüşıöçİĞÜŞÖÇ' AS tr, '' AS empty, CAST(NULL AS varbinary(1)) AS nb,
        CAST(NULL AS datetimeoffset) AS ndto, CAST(5 AS sql_variant) AS v_int, CAST(N'ş' AS sql_variant) AS v_str,
        CAST(CAST(3 AS bigint) AS sql_variant) AS v_big, CAST(CAST(2 AS tinyint) AS sql_variant) AS v_tiny,
        CAST(CAST(-4 AS smallint) AS sql_variant) AS v_small, SERVERPROPERTY('EngineEdition') AS engine_edition,
        CAST('/1/2/' AS hierarchyid) AS hid, CAST('/1/2/' AS hierarchyid).ToString() AS hid_text,
        geography::Point(41.0, 29.0, 4326).STAsText() AS geo_text, geography::Point(41.0, 29.0, 4326) AS geo,
        CAST(0x01 AS rowversion) AS rv, CAST(N'nt' AS ntext) AS ntx, CAST(1.5 AS smallmoney) AS sm""")
    r = dict(zip(res["result_sets"][0]["columns"], rows(res)[0]))
    assert r["big"] == 12345678901234 and r["f"] == 1.25 and r["r"] == 2.5
    assert r["m"] == "123.4567" and r["d"] == "99999999999999999.99"
    assert r["b"] is True and r["ti"] == 255
    assert r["dt2"].startswith("2024-02-29T13:14:15.123456")
    assert r["dto"].startswith("2024-02-29T13:14:15.123") and r["dto"].endswith("+03:00")
    assert r["dd"] == "2024-02-29" and r["tt"].startswith("23:59:59.5") and r["sdt"] == "2024-02-29T10:00:00"
    assert r["g"] == "6F9619FF-8B86-D011-B42D-00C04FC964FF"
    assert r["vb"] == "0x00FF10" and r["n"] is None and r["nb"] is None
    assert "ş" in r["x"] and r["tr"] == "ğüşıöçİĞÜŞÖÇ" and r["empty"] == ""
    assert r["ndto"] is None and r["v_int"] == 5 and r["v_str"] == "ş" and r["v_big"] == 3
    assert r["v_tiny"] == 2 and r["v_small"] == -4 and r["engine_edition"] == 3
    assert r["hid"].startswith("0x") and r["hid_text"] == "/1/2/" and r["geo_text"] == "POINT (29 41)"
    assert r["geo"].startswith("0xE6100000") and r["rv"] == "0x0100000000000000"
    assert r["ntx"] == "nt" and r["sm"] == "1.5000"


def test_unicode_parameters_round_trip(sql):
    sql("CREATE TABLE dbo.U (t nvarchar(200), v varchar(200))")
    text = "Şirket çalışanı İĞÜÖ 😀 €"
    sql("INSERT dbo.U VALUES (?, ?)", params=[text, "plain ascii"])
    assert rows(sql("SELECT t, v FROM dbo.U")) == [[text, "plain ascii"]]
    assert rows(sql("SELECT COUNT(*) FROM dbo.U WHERE t = ?", params=[text])) == [[1]]


def test_null_and_typed_parameters(sql):
    res = sql("SELECT ? AS a, ? AS b, ? AS c, ? AS d", params=[None, 1.5, True, "2024-01-01"])
    assert rows(res) == [[None, 1.5, True, "2024-01-01"]]


def test_large_nvarchar_max(sql):
    res = sql("SELECT REPLICATE(CAST(N'ab' AS nvarchar(max)), 100000) AS big")
    assert len(rows(res)[0][0]) == 200000


# ====================================================================== batches, messages, errors

def test_multiple_result_sets_messages_rowcounts(sql):
    res = sql("CREATE TABLE #t (a int); PRINT 'hello world'; SELECT 1 AS x; INSERT #t VALUES (1),(2); "
              "SELECT a FROM #t ORDER BY a; RAISERROR('just info', 10, 1);")
    assert [rs["columns"] for rs in res["result_sets"]] == [["x"], ["a"]]
    assert rows(res, 1) == [[1], [2]]
    assert "hello world" in res["messages"] and "just info" in res["messages"]
    assert res["rows_affected"]  # driver-dependent granularity, but present


def test_go_batches_and_procedure_creation(sql):
    res = sql("""
CREATE TABLE dbo.G (id int)
GO
CREATE PROCEDURE dbo.AddG @id int AS
BEGIN
    SET NOCOUNT ON;
    -- GO inside a comment must not split
    INSERT dbo.G VALUES (@id);
    SELECT 'GO
inside string' AS s;
END
GO
EXEC dbo.AddG 1
GO 3
SELECT COUNT(*) AS n FROM dbo.G
""")
    assert res["batches"] == 6
    assert rows(res, 3) == [[3]]
    assert rows(res, 0) == [["GO\ninside string"]]


def test_syntax_error(sql, ctx):
    e = err(call(T.execute_sql, sql="SELECT FROM WHERE", database=TEST_DB))
    assert "syntax" in e["message"].lower()
    if ctx["driver"] == "pyodbc":
        assert e["sql_error_number"] in (102, 156)


def test_invalid_object_error(ctx):
    e = err(call(T.execute_sql, sql="SELECT * FROM dbo.DoesNotExist", database=TEST_DB))
    assert "Invalid object name" in e["message"]


def test_runtime_error_after_partial_results(ctx):
    res = call(T.execute_sql, sql="SELECT 1 AS ok; SELECT 1/0 AS boom;", database=TEST_DB)
    assert res["success"] is False and "Divide by zero" in res["errors"][0]["message"]
    assert rows(res) == [[1]]


def test_raiserror_and_throw(ctx):
    e = err(call(T.execute_sql, sql="RAISERROR('custom %s %d', 16, 1, N'oops', 42)"))
    assert "custom oops 42" in e["message"]
    e = err(call(T.execute_sql, sql="THROW 50001, N'Thrown ş', 1;"))
    assert "Thrown ş" in e["message"]


def test_error_in_middle_batch_stops_or_continues(sql, ctx):
    sql("CREATE TABLE dbo.B (a int)")
    script = "INSERT dbo.B VALUES (1)\nGO\nINSERT dbo.Nope VALUES (1)\nGO\nINSERT dbo.B VALUES (3)"
    res = call(T.execute_sql, sql=script, database=TEST_DB)
    assert res["success"] is False and res["errors"][0]["batch"] == 2
    assert rows(sql("SELECT a FROM dbo.B")) == [[1]]
    res = call(T.execute_sql, sql=script, database=TEST_DB, continue_on_error=True)
    assert res["success"] is False and len(res["errors"]) == 1
    assert rows(sql("SELECT a FROM dbo.B ORDER BY a")) == [[1], [1], [3]]


def test_empty_and_comment_only_sql(ctx):
    assert "empty" in err(call(T.execute_sql, sql="   \n GO \n"))["message"]
    res = call(T.execute_sql, sql="-- nothing here")
    assert res["success"] and res["result_sets"] == []


def test_params_with_multiple_batches_rejected(ctx):
    e = err(call(T.execute_sql, sql="SELECT ?\nGO\nSELECT ?", params=[1]))
    assert "single batch" in e["message"]


def test_wrong_param_count(ctx):
    res = call(T.execute_sql, sql="SELECT ?, ?", params=[1])
    assert res["success"] is False


def test_max_rows_truncation_and_boundary(sql):
    q = "SELECT TOP (?) ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS n FROM sys.all_objects"
    res = sql(q, params=[10], max_rows=10)
    assert res["result_sets"][0]["row_count"] == 10 and "truncated" not in res["result_sets"][0]
    res = sql(q, params=[11], max_rows=10)
    assert res["result_sets"][0]["row_count"] == 10 and res["result_sets"][0]["truncated"] is True
    res = sql(q, params=[2000], max_rows=1)
    assert rows(res) == [[1]] and res["result_sets"][0]["truncated"]


def test_markdown_output(sql):
    sql("CREATE TABLE dbo.Md (a int, b nvarchar(20))")
    sql("INSERT dbo.Md VALUES (1, N'x|y'), (2, NULL)")
    text = call(T.execute_sql, sql="SELECT a, b FROM dbo.Md ORDER BY a", database=TEST_DB, output_format="markdown")
    assert "| a | b |" in text and "| 1 | x\\|y |" in text and "| 2 | NULL |" in text
    text = call(T.execute_sql, sql="SELECT * FROM nope", database=TEST_DB, output_format="markdown")
    assert "**ERROR**" in text
    assert "output_format" in err(call(T.execute_sql, sql="SELECT 1", output_format="xml"))["message"]


def test_query_timeout_and_connection_still_usable(ctx):
    started = time.monotonic()
    res = call(T.execute_sql, sql="WAITFOR DELAY '00:00:06'; SELECT 1", timeout_seconds=1)
    assert res["success"] is False and time.monotonic() - started < 5.5
    res = call(T.execute_sql, sql="SELECT 42 AS answer")
    assert res["success"] and rows(res) == [[42]]


# ====================================================================== database context & session state

def test_database_parameter_restores_context(ctx):
    res = call(T.execute_sql, sql="SELECT DB_NAME() AS db", database=TEST_DB)
    assert rows(res) == [[TEST_DB]]
    res = call(T.execute_sql, sql="SELECT DB_NAME() AS db")
    assert rows(res) == [["master"]]


def test_nonexistent_database(ctx):
    e = err(call(T.execute_sql, sql="SELECT 1", database="NoSuchDb_xyz"))
    assert "NoSuchDb_xyz" in e["message"]
    assert rows(call(T.execute_sql, sql="SELECT DB_NAME()")) == [["master"]]


def test_temp_table_persists_between_calls(ctx):
    assert call(T.execute_sql, sql="CREATE TABLE #persist (a int); INSERT #persist VALUES (7);")["success"]
    assert rows(call(T.execute_sql, sql="SELECT a FROM #persist")) == [[7]]
    call(T.execute_sql, sql="DROP TABLE #persist")


def test_concurrent_calls_are_serialised(ctx):
    async def many():
        return await asyncio.gather(*[T.execute_sql(sql=f"SELECT {i} AS i") for i in range(15)])
    results = [json.loads(t) for t in asyncio.run(many())]
    assert sorted(r["result_sets"][0]["rows"][0][0] for r in results) == list(range(15))


# ====================================================================== stored procedures

def test_stored_procedure_full_cycle(sql, ctx):
    sql("""CREATE TABLE dbo.Orders (Id int IDENTITY PRIMARY KEY, Customer nvarchar(50), Amount decimal(18,2))
GO
CREATE PROCEDURE dbo.AddOrder @Customer nvarchar(50), @Amount decimal(18,2),
    @NewId int OUTPUT, @Total decimal(18,2) OUTPUT, @Counter int = 0 OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    IF @Amount <= 0 BEGIN RAISERROR('Amount must be positive', 16, 1); RETURN -1; END
    INSERT dbo.Orders (Customer, Amount) VALUES (@Customer, @Amount);
    SET @NewId = SCOPE_IDENTITY();
    SELECT @Total = SUM(Amount) FROM dbo.Orders WHERE Customer = @Customer;
    SET @Counter = @Counter + 1;
    SELECT Id, Customer, Amount FROM dbo.Orders WHERE Id = @NewId;
    RETURN 7;
END""")
    res = call(T.execute_procedure, procedure="dbo.AddOrder", database=TEST_DB,
               params={"Customer": "Ömer", "@Amount": 12.5, "Counter": 10},
               output_params={"NewId": "int", "Total": "decimal(18,2)", "Counter": "int"})
    assert res["success"], res
    assert res["return_value"] == 7
    assert res["output_params"] == {"NewId": 1, "Total": "12.50", "Counter": 11}
    assert rows(res) == [[1, "Ömer", "12.50"]]
    res = call(T.execute_procedure, procedure="[dbo].[AddOrder]", database=TEST_DB,
               params={"Customer": "Ömer", "Amount": 7.5}, output_params={"NewId": "int", "Total": "decimal(18,2)"})
    assert res["output_params"]["Total"] == 20 and res["output_params"]["NewId"] == 2
    # business error raised by the procedure
    res = call(T.execute_procedure, procedure="dbo.AddOrder", database=TEST_DB,
               params={"Customer": "x", "Amount": -1}, output_params={"NewId": "int", "Total": "money"})
    assert "Amount must be positive" in err(res)["message"]
    # missing required parameter
    res = call(T.execute_procedure, procedure="dbo.AddOrder", database=TEST_DB, params={"Customer": "x"})
    assert "expects parameter" in err(res)["message"]
    # positional + no outputs (return value only)
    sql("CREATE PROCEDURE dbo.Echo @a int, @b nvarchar(10) AS SELECT @a AS a, @b AS b; RETURN @a * 2;")
    res = call(T.execute_procedure, procedure="dbo.Echo", database=TEST_DB, params=[21, "ş"])
    assert res["return_value"] == 42 and rows(res) == [[21, "ş"]] and "output_params" not in res
    # ALTER, definition, DROP
    sql("ALTER PROCEDURE dbo.Echo @a int, @b nvarchar(10) AS SELECT @b AS b, @a AS a; RETURN 0;")
    d = call(T.get_object_definition, name="dbo.Echo", database=TEST_DB)
    assert d["success"], d
    assert "SELECT @b AS b" in d["definition"] and d["type"] == "SQL_STORED_PROCEDURE"
    sql("DROP PROCEDURE dbo.Echo")
    assert "Could not find stored procedure" in err(call(T.execute_procedure, procedure="dbo.Echo",
                                                         database=TEST_DB))["message"]


def test_system_procedure_and_three_part_name(ctx):
    res = call(T.execute_procedure, procedure="sys.sp_who", params={"loginame": "sa"})
    assert res["success"] and res["result_sets"]
    res = call(T.execute_procedure, procedure=f"{TEST_DB}.sys.sp_helpfile")
    assert res["success"] and res["result_sets"]


# ====================================================================== transactions

def test_transaction_rollback_and_commit(sql):
    sql("CREATE TABLE dbo.Tx (a int)")
    assert call(T.begin_transaction)["success"]
    status = call(T.transaction_status)
    assert rows(status)[0][0] == 1
    res = sql("INSERT dbo.Tx VALUES (1)")
    assert res["open_transactions"] == 1 and "commit_transaction" in res["warning"]
    assert call(T.list_connections)["connections"][0]["open_transactions"] == 1
    assert call(T.rollback_transaction)["success"]
    assert rows(sql("SELECT COUNT(*) FROM dbo.Tx")) == [[0]]
    call(T.begin_transaction, isolation_level="serializable")
    assert rows(call(T.transaction_status))[0][2] == "SERIALIZABLE"
    sql("INSERT dbo.Tx VALUES (2)")
    sql("BEGIN TRAN; INSERT dbo.Tx VALUES (3);")  # nested
    assert rows(call(T.transaction_status))[0][0] == 2
    res = call(T.commit_transaction)
    assert res["success"] and "warning" not in res
    assert rows(sql("SELECT a FROM dbo.Tx ORDER BY a")) == [[2], [3]]
    # commit / rollback without an open transaction are no-ops
    assert call(T.commit_transaction)["success"] and call(T.rollback_transaction)["success"]
    call(T.begin_transaction, isolation_level="read committed")
    call(T.rollback_transaction)
    assert "isolation" in err(call(T.begin_transaction, isolation_level="chaos"))["message"].lower()


def test_xact_abort_error_inside_transaction(sql):
    sql("CREATE TABLE dbo.Tx2 (a int PRIMARY KEY)")
    call(T.begin_transaction)
    sql("INSERT dbo.Tx2 VALUES (1)")
    res = call(T.execute_sql, sql="SET XACT_ABORT ON; INSERT dbo.Tx2 VALUES (1);", database=TEST_DB)
    assert "PRIMARY KEY" in err(res)["message"]
    assert rows(call(T.transaction_status))[0][0] == 0  # doomed tx was rolled back
    call(T.execute_sql, sql="SET XACT_ABORT OFF")
    assert rows(sql("SELECT COUNT(*) FROM dbo.Tx2")) == [[0]]


# ====================================================================== schema exploration

def test_schema_tools(sql):
    sql("""CREATE SCHEMA sales
GO
CREATE TABLE sales.Region (Id int PRIMARY KEY, Name nvarchar(40) NOT NULL UNIQUE)
GO
CREATE TABLE sales.Invoice (
    Id int IDENTITY(1,1) CONSTRAINT PK_Invoice PRIMARY KEY,
    RegionId int NOT NULL CONSTRAINT FK_Invoice_Region REFERENCES sales.Region(Id) ON DELETE CASCADE,
    Amount decimal(10,3) NOT NULL CONSTRAINT DF_Amount DEFAULT (0),
    Code varchar(max) NULL, Note nvarchar(25) NULL, Ts datetime2(3) NOT NULL DEFAULT SYSDATETIME(),
    Doubled AS (Amount * 2))
GO
CREATE INDEX IX_Invoice_Region ON sales.Invoice (RegionId DESC) INCLUDE (Amount) WHERE Amount > 0
GO
CREATE VIEW sales.vInvoice AS SELECT Id, Amount FROM sales.Invoice
GO
CREATE FUNCTION sales.fnDouble (@x int) RETURNS int AS BEGIN RETURN @x * 2 END
GO
INSERT sales.Region VALUES (1, N'Marmara')""")
    res = call(T.list_databases)
    assert TEST_DB in [r[0] for r in rows(res)]
    res = call(T.list_objects, database=TEST_DB, schema="sales")
    names = {(r[1], r[2]) for r in rows(res)}
    assert {("Region", "USER_TABLE"), ("Invoice", "USER_TABLE"), ("vInvoice", "VIEW"),
            ("fnDouble", "SQL_SCALAR_FUNCTION")} <= names
    region = next(r for r in rows(res) if r[1] == "Region")
    assert region[5] == 1
    res = call(T.list_objects, database=TEST_DB, object_type="views")
    assert [r[1] for r in rows(res)] == ["vInvoice"]
    res = call(T.list_objects, database=TEST_DB, object_type="function", name_like="fn%")
    assert [r[1] for r in rows(res)] == ["fnDouble"]
    assert "object_type" in err(call(T.list_objects, object_type="widget"))["message"]

    d = call(T.describe_table, table="sales.Invoice", database=TEST_DB)
    assert d["success"], d
    cols = {c["column_name"]: c for c in d["columns"]}
    assert cols["Id"]["is_identity"] is True and cols["Amount"]["data_type"] == "decimal(10,3)"
    assert cols["Code"]["data_type"] == "varchar(max)" and cols["Note"]["data_type"] == "nvarchar(25)"
    assert cols["Ts"]["data_type"] == "datetime2(3)" and cols["Amount"]["default_value"] == "((0))"
    assert cols["Doubled"]["is_computed"] is True and "Amount" in cols["Doubled"]["computed_definition"]
    pk = next(i for i in d["indexes"] if i["is_primary_key"])
    assert pk["index_name"] == "PK_Invoice" and pk["key_columns"] == "Id"
    ix = next(i for i in d["indexes"] if i["index_name"] == "IX_Invoice_Region")
    assert ix["key_columns"] == "RegionId DESC" and ix["included_columns"] == "Amount" and ix["filter_definition"]
    assert d["foreign_keys"][0]["references_table"] == "sales.Region" and d["foreign_keys"][0]["on_delete"] == "CASCADE"
    assert d["row_count"] == 0
    # three-part name, view, not found, injection attempt
    assert call(T.describe_table, table=f"{TEST_DB}.sales.Region")["row_count"] == 1
    assert call(T.describe_table, table="sales.vInvoice", database=TEST_DB)["success"]
    assert "was not found" in err(call(T.describe_table, table="sales.Nope", database=TEST_DB))["message"]
    assert "was not found" in err(call(T.describe_table, table="sales.fnDouble", database=TEST_DB))["message"]
    res = call(T.describe_table, table="x]; DROP TABLE sales.Region; --", database=TEST_DB)
    assert res["success"] is False
    assert rows(call(T.execute_sql, sql="SELECT COUNT(*) FROM sales.Region", database=TEST_DB)) == [[1]]

    v = call(T.get_object_definition, name="sales.vInvoice", database=TEST_DB)
    assert "CREATE VIEW sales.vInvoice" in v["definition"]
    t = call(T.get_object_definition, name="sales.Invoice", database=TEST_DB)
    assert t["definition"] is None and "describe_table" in t["note"]
    assert "was not found" in err(call(T.get_object_definition, name="sales.Zzz", database=TEST_DB))["message"]


# ====================================================================== admin operations

def test_admin_operations(sql, ctx):
    login = "sqlmcp_reader"
    pw = "Rd!" + "x9" * 6
    res = call(T.execute_sql, sql=f"""
IF SUSER_ID('{login}') IS NOT NULL DROP LOGIN [{login}];
CREATE LOGIN [{login}] WITH PASSWORD = N'{pw}', CHECK_POLICY = OFF;
""")
    assert res["success"], res
    sql(f"""CREATE USER [{login}] FOR LOGIN [{login}];
CREATE TABLE dbo.Secret (a int); CREATE TABLE dbo.PublicData (a int);
INSERT dbo.PublicData VALUES (1);
GRANT SELECT ON dbo.PublicData TO [{login}];""")
    sql(f"ALTER DATABASE [{TEST_DB}] SET RECOVERY FULL")
    res = call(T.list_databases)
    rec = next(r for r in rows(res) if r[0] == TEST_DB)
    assert rec[3] == "FULL"
    res = sql(f"BACKUP DATABASE [{TEST_DB}] TO DISK = N'/var/opt/mssql/data/{TEST_DB}.bak' WITH INIT, COPY_ONLY")
    assert any("BACKUP DATABASE successfully" in m for m in res.get("messages", []))
    res = sql("DBCC CHECKDB WITH NO_INFOMSGS")
    assert res["success"]
    res = call(T.execute_sql, sql="EXEC sp_configure 'show advanced options'")
    assert res["success"] and rows(res)[0][0] == "show advanced options"

    # connect as the limited login -> permission errors are reported cleanly
    ctx["mgr"].add(make_profile(ctx["target"], ctx["driver"], name="reader", user=login, password=pw,
                                database=TEST_DB))
    try:
        res = call(T.execute_sql, sql="SELECT a FROM dbo.PublicData", connection="reader")
        assert rows(res) == [[1]]
        e = err(call(T.execute_sql, sql="SELECT a FROM dbo.Secret", connection="reader"))
        assert "permission was denied" in e["message"]
        e = err(call(T.create_job, job_name="nope", steps=[{"name": "s", "command": "SELECT 1"}],
                     connection="reader"))
        assert e["message"]
        info = call(T.test_connection, connection="reader")
        assert info["success"], info
        info = info["server"]
        assert info["is_sysadmin"] == 0 and info["login_name"] == login and info["auth_scheme"] is None
        assert info["sql_agent"].startswith("Unknown")
    finally:
        ctx["mgr"].remove("reader")
    call(T.execute_sql, sql=f"DROP LOGIN [{login}]")


def test_readonly_profile(sql, ctx):
    sql("CREATE TABLE dbo.Ro (a int); INSERT dbo.Ro VALUES (5);")
    ctx["mgr"].add(make_profile(ctx["target"], ctx["driver"], name="ro", readonly=True, database=TEST_DB))
    try:
        assert rows(call(T.execute_sql, sql="SELECT a FROM dbo.Ro", connection="ro")) == [[5]]
        assert call(T.execute_sql, sql="EXEC sp_who2", connection="ro")["success"]
        for bad in ("DELETE dbo.Ro", "UPDATE dbo.Ro SET a = 1", "DROP TABLE dbo.Ro", "EXEC dbo.Whatever",
                    "SELECT * INTO dbo.Copy FROM dbo.Ro"):
            e = err(call(T.execute_sql, sql=bad, connection="ro"))
            assert e["type"] == "ReadOnlyViolation", bad
        e = err(call(T.create_job, job_name="x", steps=[{"name": "s", "command": "SELECT 1"}], connection="ro"))
        assert e["type"] == "ReadOnlyViolation"
        assert err(call(T.kill_session, session_id=999, connection="ro"))["type"] == "ReadOnlyViolation"
        assert err(call(T.execute_procedure, procedure="dbo.X", connection="ro"))["type"] == "ReadOnlyViolation"
        assert call(T.list_jobs, connection="ro")["success"]
        assert rows(call(T.execute_sql, sql="SELECT COUNT(*) FROM dbo.Ro", connection="ro")) == [[1]]
    finally:
        ctx["mgr"].remove("ro")


def test_sessions_and_kill(ctx):
    other = SessionManager({"o": make_profile(ctx["target"], ctx["driver"], name="o")}, "o")
    sess = other.get("o")
    res = sess.execute("SELECT @@SPID AS spid")
    spid = res["result_sets"][0]["rows"][0][0]
    sess.execute("BEGIN TRAN")  # gives it an open transaction so only_active lists it
    listed = call(T.list_sessions)
    assert spid in [r[0] for r in rows(listed)]
    assert call(T.list_sessions, only_active=False, include_system=True)["success"]
    own = rows(call(T.execute_sql, sql="SELECT @@SPID"))[0][0]
    assert "own session" in err(call(T.kill_session, session_id=own))["message"]
    assert "positive" in err(call(T.kill_session, session_id=0))["message"]
    assert call(T.kill_session, session_id=spid)["success"]
    # the killed connection notices and refuses to silently drop its transaction
    session.PING_AFTER_IDLE_SECONDS = 0
    try:
        with pytest.raises(session.SessionError):
            sess.execute("SELECT 1")
        res = sess.execute("SELECT 1 AS again")  # next call reconnects
        assert res["success"] and res["result_sets"][0]["rows"] == [[1]]
    finally:
        session.PING_AFTER_IDLE_SECONDS = 15.0
        other.close_all()


def test_reconnect_after_server_side_kill(ctx):
    own = rows(call(T.execute_sql, sql="SELECT @@SPID"))[0][0]
    killer = SessionManager({"k": make_profile(ctx["target"], ctx["driver"], name="k")}, "k")
    try:
        assert killer.get("k").execute(f"KILL {own}")["success"]
    finally:
        killer.close_all()
    session.PING_AFTER_IDLE_SECONDS = 0
    try:
        res = call(T.execute_sql, sql="SELECT @@SPID AS spid")
        assert res["success"], res
        assert res["result_sets"][0]["rows"][0][0] > 0
    finally:
        session.PING_AFTER_IDLE_SECONDS = 15.0


# ====================================================================== SQL Server Agent jobs

def _wait(predicate, timeout=60, interval=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


@pytest.fixture
def job_names(ctx):
    names = []
    yield names
    for n in names:
        call(T.execute_sql, sql="IF EXISTS (SELECT 1 FROM msdb.dbo.sysjobs WHERE name = ?) "
                                "EXEC msdb.dbo.sp_delete_job @job_name = ?", params=[n, n])


def test_job_lifecycle(sql, ctx, job_names):
    name = f"sqlmcp test job {ctx['driver']} ş"
    job_names.append(name)
    job_names.append(name + " renamed")
    sql("CREATE TABLE dbo.JobLog (msg nvarchar(100), at datetime2 DEFAULT SYSDATETIME())")
    res = call(T.create_job, job_name=name, description="created by tests", steps=[
        {"name": "write log", "command": "INSERT dbo.JobLog (msg) VALUES (N'step1');", "database": TEST_DB},
        {"name": "second", "command": "INSERT dbo.JobLog (msg) VALUES (N'step2');", "database": TEST_DB,
         "retry_attempts": 1, "retry_interval": 1},
    ], schedules=[
        {"name": "nightly", "type": "daily", "start_time": "02:30"},
        {"name": "weekdays every 15m", "type": "weekly", "days": ["mon", "tue", "wed", "thu", "fri"],
         "repeat_every_minutes": 15, "start_time": "08:00", "end_time": "18:00"},
    ])
    assert res["success"], res
    job_id = rows(res)[0][0]
    assert len(job_id) == 36

    j = call(T.get_job, job_name=name)
    assert j["success"], j
    assert j["job"]["description"] == "created by tests" and j["job"]["enabled"] == 1
    assert [(s["step_id"], s["step_name"], s["on_success"]) for s in j["steps"]] == [
        (1, "write log", "next"), (2, "second", "quit_success")]
    assert j["steps"][1]["retry_attempts"] == 1
    scheds = {s["schedule_name"]: s for s in j["schedules"]}
    assert scheds["nightly"]["type"] == "daily" and scheds["nightly"]["active_start_time"] == 23000
    assert scheds["weekdays every 15m"]["freq_interval"] == 62 and scheds["weekdays every 15m"]["freq_subday_interval"] == 15

    listed = call(T.list_jobs, name_like="sqlmcp test job%")
    assert name in [r[0] for r in rows(listed)]

    # duplicate name -> whole creation rolled back, original untouched
    dup = call(T.create_job, job_name=name, steps=[{"name": "x", "command": "SELECT 1"}])
    assert "already exists" in err(dup)["message"]
    assert len(call(T.get_job, job_name=name)["steps"]) == 2

    # run it and wait for success in history
    assert call(T.start_job, job_name=name)["success"]
    hist = _wait(lambda: [r for r in rows(call(T.get_job_history, job_name=name)) if r[1] == 0])
    assert hist[0][4] == "Succeeded"
    assert [r[0] for r in rows(sql("SELECT msg FROM dbo.JobLog ORDER BY at"))] == ["step1", "step2"]
    lj = next(r for r in rows(call(T.list_jobs, name_like=name)) if r[0] == name)
    assert lj[8] == "Succeeded"

    # modify job / steps / schedules
    assert call(T.update_job, job_name=name, enabled=False, description="changed")["success"]
    assert call(T.add_job_step, job_name=name, step={"name": "third", "command": "SELECT 3"})["success"]
    assert call(T.update_job_step, job_name=name, step_id=2,
                changes={"command": "SELECT 2", "on_success": "next", "on_fail": "goto:3"})["success"]
    assert call(T.add_job_schedule, job_name=name, schedule={"name": "monthly", "type": "monthly",
                                                             "day_of_month": 1, "start_time": "05:00"})["success"]
    assert call(T.delete_job_schedule, job_name=name, schedule_name="nightly")["success"]
    j = call(T.get_job, job_name=name)
    assert j["job"]["enabled"] == 0 and j["job"]["description"] == "changed"
    assert [s["step_name"] for s in j["steps"]] == ["write log", "second", "third"]
    assert j["steps"][1]["command"] == "SELECT 2" and j["steps"][1]["on_fail"] == "goto:3"
    assert sorted(s["schedule_name"] for s in j["schedules"]) == ["monthly", "weekdays every 15m"]
    assert call(T.delete_job_step, job_name=name, step_id=3)["success"]
    assert len(call(T.get_job, job_name=name)["steps"]) == 2
    assert call(T.list_jobs, name_like=name, enabled=False)["result_sets"][0]["row_count"] == 1
    assert call(T.list_jobs, name_like=name, enabled=True)["result_sets"][0]["row_count"] == 0

    # rename and delete
    assert call(T.update_job, job_name=name, new_name=name + " renamed")["success"]
    assert "was not found" in err(call(T.get_job, job_name=name))["message"]
    assert call(T.delete_job, job_name=name + " renamed")["success"]
    assert call(T.list_jobs, name_like=name + "%")["result_sets"][0]["row_count"] == 0


def test_failing_job_and_stop_running_job(sql, ctx, job_names):
    fail = f"sqlmcp failing {ctx['driver']}"
    slow = f"sqlmcp slow {ctx['driver']}"
    job_names.extend([fail, slow])
    assert call(T.create_job, job_name=fail, steps=[
        {"name": "boom", "command": "SELECT 1/0;"}])["success"]
    assert call(T.start_job, job_name=fail)["success"]
    hist = _wait(lambda: rows(call(T.get_job_history, job_name=fail, failed_only=True)))
    assert all(r[4] == "Failed" for r in hist)
    assert any("Divide by zero" in (r[8] or "") for r in hist)

    assert call(T.create_job, job_name=slow, steps=[
        {"name": "wait", "command": "WAITFOR DELAY '00:02:00';"}])["success"]
    assert call(T.start_job, job_name=slow)["success"]
    running = _wait(lambda: [r for r in rows(call(T.list_running_jobs)) if r[0] == slow])
    assert running[0][3] == 1
    # starting again while running is an error from SQL Agent
    assert "already running" in err(call(T.start_job, job_name=slow))["message"]
    assert call(T.stop_job, job_name=slow)["success"]
    _wait(lambda: not [r for r in rows(call(T.list_running_jobs)) if r[0] == slow])
    assert "not currently running" in err(call(T.stop_job, job_name=slow))["message"]


def test_job_error_cases(ctx, job_names):
    assert "was not found" in err(call(T.get_job, job_name="no such job zz"))["message"]
    assert err(call(T.start_job, job_name="no such job zz"))["message"]
    assert err(call(T.delete_job, job_name="no such job zz"))["message"]
    assert "was not found" in err(call(T.get_job_history, job_name="no such job zz"))["message"]
    assert "top" in err(call(T.get_job_history, top=0))["message"]
    e = err(call(T.create_job, job_name="sqlmcp bad schedule", steps=[{"name": "s", "command": "SELECT 1"}],
                 schedules=[{"name": "x", "type": "weekly"}]))
    assert e["type"] == "JobSpecError"
    # invalid subsystem -> server error, and nothing half-created
    name = f"sqlmcp bad subsystem {ctx['driver']}"
    job_names.append(name)
    e = err(call(T.create_job, job_name=name, steps=[{"name": "s", "command": "x", "subsystem": "Nope"}]))
    assert e["message"]
    assert call(T.list_jobs, name_like=name)["result_sets"][0]["row_count"] == 0
    assert call(T.get_job_history, top=5)["success"]
    assert "Nothing to update" in err(call(T.update_job, job_name="x"))["message"]
