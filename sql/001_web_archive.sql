USE [RehabPatients-ylh];
GO

IF OBJECT_ID(N'dbo.Patients', N'U') IS NULL
CREATE TABLE dbo.Patients (
    PatientID nvarchar(50) NOT NULL PRIMARY KEY,
    Name nvarchar(100) NOT NULL,
    Sex nvarchar(10) NOT NULL DEFAULT N'未指定',
    Notes nvarchar(1000) NOT NULL DEFAULT N'',
    CreatedAt datetime2 NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF OBJECT_ID(N'dbo.SessionArchives', N'U') IS NULL
CREATE TABLE dbo.SessionArchives (
    TestID nvarchar(50) NOT NULL PRIMARY KEY,
    PatientID nvarchar(50) NOT NULL REFERENCES dbo.Patients(PatientID),
    StartedAt nvarchar(64) NOT NULL,
    Source nvarchar(20) NOT NULL,
    Status nvarchar(20) NOT NULL,
    SessionJSON nvarchar(max) NOT NULL,
    CreatedAt datetime2 NOT NULL DEFAULT SYSUTCDATETIME()
);
GO

IF OBJECT_ID(N'dbo.RawFiles', N'U') IS NULL
CREATE TABLE dbo.RawFiles (
    TestID nvarchar(50) NOT NULL REFERENCES dbo.SessionArchives(TestID) ON DELETE CASCADE,
    Kind nvarchar(20) NOT NULL,
    FileName nvarchar(255) NOT NULL,
    Content varbinary(max) NOT NULL,
    CONSTRAINT PK_RawFiles PRIMARY KEY (TestID, Kind)
);
GO

INSERT INTO dbo.Patients (PatientID, Name, Sex)
SELECT s.PatientID, COALESCE(MAX(NULLIF(s.Name, N'')), s.PatientID),
       COALESCE(MAX(NULLIF(s.Sex, N'')), N'未指定')
FROM dbo.Sessions s
WHERE NOT EXISTS (SELECT 1 FROM dbo.Patients p WHERE p.PatientID=s.PatientID)
GROUP BY s.PatientID;
GO
