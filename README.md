# Stf.GoTest — Go-style tests in a single `.csproj`

[![CI](https://github.com/tonyredondo/Stf.GoTest/actions/workflows/ci.yml/badge.svg)](https://github.com/tonyredondo/Stf.GoTest/actions/workflows/ci.yml)

A NuGet package that brings Go's test model to .NET: code and its tests live
in the **same project** — no second test project, no `InternalsVisibleTo`.
`*.Test.cs` files compile into the test slice and are excluded from prod.

- Current version: `0.1.53` (local feed: `artifacts/packages`).
- Working demo: [`demo/Demo`](demo/Demo) (`Calculator.cs` + `Calculator.Test.cs`).

## Onboarding (3 steps)

**1. Package reference** (unconditional, always):

```xml
<PackageReference Include="Stf.GoTest" Version="0.1.53" PrivateAssets="all" />
```

**2. The test snippet** (the only copy-paste; see below for why each piece):

```xml
<ItemGroup Condition="'$(StfTest)' != 'false'">
  <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.11.1" PrivateAssets="all" GeneratePathProperty="true" />
  <PackageReference Include="xunit" Version="2.4.2" PrivateAssets="all" />
  <PackageReference Include="xunit.runner.visualstudio" Version="2.4.3" PrivateAssets="all" />
</ItemGroup>
```

**3. Write `Thing.Test.cs`** next to `Thing.cs` (singular, like Go's `_test.go`;
plural `*.Tests.cs` is also excluded from prod, matching case-insensitively
so lowercase `*.test.cs` strips too). Tests see `internal`s
directly: same assembly, no hacks.

**Exe projects**: add one line after `<OutputType>` (see [Exes](#exes)):

```xml
<OutputType>Exe</OutputType>
<StfTest>false</StfTest>
```

## Behavior table

`StfTest` defaults to `true` (via `-p:`, environment variable, or a body line).
`1/0/yes/no/on/off` spellings are accepted for `StfTest` and every flag below
(`StfSeparateOutputs`, `StfDualBuild`, all `StfAllow*`). Golden rule:
**everything ships prod by default; test only shows up where it belongs**.

### `dotnet build`

| Project | no flag | `StfTest=false` | `StfTest=true` |
|---|---|---|---|
| Lib without `Program.cs` | dual: TEST `bin/test` + PROD `bin/` | PROD only | same as no flag |
| Exe with `Main` in `Program.cs` | dual (entry auto-dropped from test slice) | PROD only | dual |
| Exe with `Main` outside `Program.cs` | actionable error (backstop) | PROD | actionable error |
| Exe with old snippet | bare CS0017 ([migrate](#old-snippet)) | PROD | bare CS0017 |
| Lib with Main-less `Program.cs` | green | PROD | green |
| `WinExe` | loud guard on all 3 CI OSes (a degraded restore falls back to the upstream error, still loud) | PROD | same |

### `dotnet test` / `dotnet pack` / `dotnet publish` / `dotnet run`

| Command | no flag | `StfTest=false` |
|---|---|---|
| `test` (lib or exe) | runs the tests | builds prod, 0 tests, silent exit 0 |
| `pack` / `publish` (lib or exe) | auto-redirect: **PROD only** | direct PROD |
| `run` (exe) | **runs the prod app** | runs the app |
| `run` (lib) | the CLI declines (`OutputType Library`) | same |

Escape hatches: `StfAllowTestSlicePack` / `StfAllowTestSlicePublish` = `true`
ship the test slice as-is. `StfDualBuild=false` disables the dual build.
`StfAllowExeTestMode=true` silences the exe guard. `StfAllowPack=false` leaves
`pack` fully alone (no prod redirect: pack behaves like a plain test project).
`StfAllowBareTestRefs=true` silences the unconditioned-ref warning (also
covers `xunit.v3`). In the IDE, `#if STF_TEST` marks test-only code.

## Why the snippet looks like this

Every piece exists because of a verified MSBuild/NuGet wall:

- **`Condition="'$(StfTest)' != 'false'"`** — graph restore cannot see
  package-provided MSBuild (`ExcludeRestorePackageImports`), so test refs must
  be conditioned in the consumer's own evaluation. Without this, the prod
  restore would include test packages (silent failure).
- **`PrivateAssets="all"`** — keeps the prod `.nuspec` clean and stops test
  deps from being copied next to the prod dll. Dual use: the test-slice
  deploy step copies exactly those suppressed references.
- **`GeneratePathProperty="true"`** (Test SDK only) — NuGet defines
  `$(PkgMicrosoft_NET_Test_Sdk)` only with this opt-in. The exe guard and the
  entry-point drop use it to detect the TestSDK without fragile constructs
  (conditions over `%()` metadata or `@()` vectors die with MSB4190/MSB4092).
  Inert in every other respect.

### Old snippet

Without `GeneratePathProperty`, the guard cannot see the TestSDK and an exe
dies with a bare CS0017 (as before the guard existed). Fix: add that metadata
to the ref. Nothing else changes.

## Exes

The TestSDK injects its own `Program.Main`, which collides with yours (CS0017)
— MSBuild cannot retry another slice after the compiler fails, so the design is:

1. **In test mode your `Program.cs` is dropped** from the compilation (the
   slice builds with the single testhost entry and `dotnet test` runs). Keep
   `Program.cs` thin (entry point only): types needed by tests belong in other
   files — referencing something declared in `Program.cs` fails loud, never
   silent. A dangling `StartupObject` is cleared alongside.
2. **Without a conventional `Program.cs`** (`Main` in another file, WPF's
   generated `Main`), the guard fails loud with the fix (the line).
3. **The `<StfTest>false</StfTest>` line** decides before restore: prod in a
   single pass, zero flags. Recommended for exes (required for `dotnet run`
   to launch the app instead of the test slice whenever ambiguous), optional
   otherwise: `build`/`test`/`pack`/`publish` work flag-free thanks to 1+2.

How a real exe is told apart from a library: the SDK sets `UseAppHost` only
when the `<OutputType>Exe` was written by the user (the `Exe` the TestSDK
imposes on libraries arrives too late to trigger it). Libraries, with or
without a `Program.cs`, never notice.

## Known limits

- `WinExe`: the guard fires on all 3 CI OSes (the backstop step prints the
  branch). A degraded restore (no `Pkg`) skips the guard, but the build still
  fails loud with the upstream compiler error. IDE behavior (VS/Rider/VSCode)
  still to verify.
- `--no-restore` right after a dual `dotnet build` fails loud (`Xunit` not
  found): the dual leaves prod assets in `obj/` and only a restore heals back
  to the test closure. Any command with restore recovers.
- `dotnet test` with `StfTest=false` goes green silently with 0 tests
  (candidate for its own warning).
- `*.Test.cs` that doesn't compile blocks `dotnet run` (the CLI's build phase
  is indistinguishable from `build`); failing asserts don't.
- AOT verified via explicit prod only
  (`StfTest=false dotnet publish -p:PublishAot=true -r <rid>` → native
  binary); trim forwards to the prod slice (warns IL1034 upstream on libraries).
- AOT publish of an exe in default test mode fails with a bare CS0017: the
  entry-point drop does not apply inside the publish evaluation (mechanism
  still open — every guard condition reads true there). Use the explicit prod
  slice above for NativeAOT.
- `StfSeparateOutputs=false` shares one output dir for both slices and skips
  the nested prod build (it would clobber the test closure); prod arrives via
  an explicit `-p:StfTest=false` build. Like `-c`, repeat the flag on every
  command (`build` and `test`), it is per-invocation.
- Multi-`TargetFramework` `dotnet pack` is a silent no-op (exit 0, packs
  nothing): the dispatcher skips package imports and the CLI never reaches
  the redirect. `dotnet publish` without `-f` fails loud instead (upstream
  NETSDK1129). Either way: ship with explicit `dotnet pack -p:StfTest=false`
  (verified pure, all TFMs).
