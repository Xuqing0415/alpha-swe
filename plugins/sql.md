---
name: sql
description: 数据库与 SQL 最佳实践（含 ORM 与索引建议）
priority: 7
version: "1.0.0"
triggers:
  keywords: [数据库, sql, db, query, 查询, 表结构, schema, orm, 建表, 索引]
  file_ext: [.sql]
  project_dep: [sqlalchemy, psycopg2, pymysql, peewee, tortoise-orm]
---
# SQL/数据库最佳实践
- 表结构变更必须提供迁移（migration）而不是手改线上表
- 查询只取需要的列，避免 SELECT *
- 为 WHERE/JOIN 频繁使用的列建立索引，但避免过度索引
- 使用参数化查询，禁止字符串拼接 SQL（防注入）
- 事务边界要短，尽量在业务层控制
- ORM 批量写入使用 bulk_create / executemany，避免循环单条插入
- 涉及数据量大的表时，先 EXPLAIN 分析执行计划再决定查询方案