-- ============================================================================
-- Barcode bot - minimal database schema
--
-- Creates the single table barcode-bot.py read and write:
--   database : Barcodes
--   table    : dbo.Item   (named Item, singular - that is what the bot's SQL
--                          expects; renaming it would break the queries)
--
-- The bot runs four statements against it:
--   SELECT Item, Brand, Category, ItemImage FROM dbo.Item  WHERE EAN13 = ?
--   INSERT INTO dbo.Item (EAN13, Category, Item, Brand, Src, RawJSON) VALUES (...)
--   UPDATE dbo.Item SET RawJSON    = ?, updatedOnTime = GETDATE() WHERE EAN13 = ?
--   UPDATE dbo.Item SET ItemImage  = ?, updatedOnTime = GETDATE() WHERE EAN13 = ?
--
-- Usage: run on your SQL Server instance, then point the bot at it:
--   MSSQL_SERVER / MSSQL_USER / MSSQL_PASSWORD / MSSQL_DATABASE=Barcodes
-- ============================================================================

IF DB_ID(N'Barcodes') IS NULL
    CREATE DATABASE Barcodes;
GO

USE Barcodes;
GO

IF OBJECT_ID(N'dbo.Item', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Item
    (
        EAN13         varchar(32)     NOT NULL,  -- the scanned code: EAN-13, UPC-A, Code-128, QR payload...
        Item          nvarchar(200)   NULL,      -- product name
        Brand         nvarchar(100)   NULL,
        Category      nvarchar(100)   NULL,
        Src           varchar(50)     NULL,      -- where the row came from ('KiwiSquare')
        RawJSON       nvarchar(max)   NULL,      -- full price-API payload; per-store offers are parsed from it
        ItemImage     varbinary(max)  NULL,      -- product photo bytes, backfilled after the first scan
        updatedOnTime datetime        NULL,      -- last refresh timestamp (bot sets GETDATE())

        CONSTRAINT PK_Item_EAN13 PRIMARY KEY (EAN13)
    );
END
GO