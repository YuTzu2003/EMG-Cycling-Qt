/* Issue #10 non-destructive SQL Server migration.
   Run this once in SSMS while connected to RehabPatients-ylh. */
USE [RehabPatients-ylh];
GO
SET XACT_ABORT ON;
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
BEGIN TRANSACTION;
GO

/* A duplicate must be resolved deliberately; this script never deletes data. */
IF EXISTS (SELECT 1 FROM dbo.EMGData GROUP BY TestID, TimeOffsetSec, MuscleName HAVING COUNT(*) > 1)
    THROW 51000, 'Cannot create UX_EMGData_TestTimeMuscle: duplicate EMG rows exist.', 1;
IF EXISTS (SELECT 1 FROM dbo.PedalData GROUP BY TestID, TimeOffsetSec HAVING COUNT(*) > 1)
    THROW 51001, 'Cannot create UX_PedalData_TestTime: duplicate pedal rows exist.', 1;
IF EXISTS (SELECT 1 FROM dbo.TrainerData GROUP BY TestID, TimeOffsetSec HAVING COUNT(*) > 1)
    THROW 51002, 'Cannot create UX_TrainerData_TestTime: duplicate trainer rows exist.', 1;
GO

IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_TestBaselines_Sessions')
    ALTER TABLE dbo.TestBaselines WITH CHECK ADD CONSTRAINT FK_TestBaselines_Sessions FOREIGN KEY (TestID) REFERENCES dbo.Sessions(TestID);
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_EMGData_Sessions')
    ALTER TABLE dbo.EMGData WITH CHECK ADD CONSTRAINT FK_EMGData_Sessions FOREIGN KEY (TestID) REFERENCES dbo.Sessions(TestID);
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_PedalData_Sessions')
    ALTER TABLE dbo.PedalData WITH CHECK ADD CONSTRAINT FK_PedalData_Sessions FOREIGN KEY (TestID) REFERENCES dbo.Sessions(TestID);
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_TrainerData_Sessions')
    ALTER TABLE dbo.TrainerData WITH CHECK ADD CONSTRAINT FK_TrainerData_Sessions FOREIGN KEY (TestID) REFERENCES dbo.Sessions(TestID);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_EMGData_TestTimeMuscle' AND object_id = OBJECT_ID('dbo.EMGData'))
    CREATE UNIQUE INDEX UX_EMGData_TestTimeMuscle ON dbo.EMGData(TestID, TimeOffsetSec, MuscleName) WHERE TimeOffsetSec IS NOT NULL AND MuscleName IS NOT NULL;
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_PedalData_TestTime' AND object_id = OBJECT_ID('dbo.PedalData'))
    CREATE UNIQUE INDEX UX_PedalData_TestTime ON dbo.PedalData(TestID, TimeOffsetSec) WHERE TimeOffsetSec IS NOT NULL;
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_TrainerData_TestTime' AND object_id = OBJECT_ID('dbo.TrainerData'))
    CREATE UNIQUE INDEX UX_TrainerData_TestTime ON dbo.TrainerData(TestID, TimeOffsetSec) WHERE TimeOffsetSec IS NOT NULL;
GO

/* Optional provenance fields needed to interpret history safely. */
IF COL_LENGTH('dbo.Sessions', 'StartedAt') IS NULL ALTER TABLE dbo.Sessions ADD StartedAt datetime NULL;
IF COL_LENGTH('dbo.Sessions', 'EndedAt') IS NULL ALTER TABLE dbo.Sessions ADD EndedAt datetime NULL;
IF COL_LENGTH('dbo.Sessions', 'AnalysisVersion') IS NULL ALTER TABLE dbo.Sessions ADD AnalysisVersion nvarchar(50) NULL;
IF COL_LENGTH('dbo.TestBaselines', 'BaselineSource') IS NULL ALTER TABLE dbo.TestBaselines ADD BaselineSource nvarchar(80) NULL;
IF COL_LENGTH('dbo.TestBaselines', 'BaselineMethod') IS NULL ALTER TABLE dbo.TestBaselines ADD BaselineMethod nvarchar(100) NULL;
GO
COMMIT TRANSACTION;
GO
