# Stf.GoTest — Go-style tests in a single `.csproj`

[![CI](https://github.com/tonyredondo/Stf.GoTest/actions/workflows/ci.yml/badge.svg)](https://github.com/tonyredondo/Stf.GoTest/actions/workflows/ci.yml)

An MSBuild SDK, distributed as a NuGet package, that brings Go's test model to
.NET: code and its tests live in the **same project** — no second test project, no `InternalsVisibleTo`.
`*.Test.cs` files compile into the test slice and are excluded from prod.

- Current version: `0.1.56` (local feed: `artifacts/packages`).
- Working demo: [`demo/Demo`](demo/Demo) (`Calculator.cs` + `Calculator.Test.cs`).

## Onboarding (3 steps)

**1. Add the SDK** after your existing .NET SDK:

```xml
<Project Sdk="Microsoft.NET.Sdk;Stf.GoTest/0.1.56">
```

For a web project, keep `Microsoft.NET.Sdk.Web` as the first SDK. Keep the
SDK version pinned. The SDK resolver uses your project's NuGet feeds; this
repository's `nuget.config` includes `artifacts/packages`.

When migrating, remove the `PackageReference` to Stf.GoTest and add the SDK
above. Keep your other project settings and existing command-line options.
This one-time project change lets Stf.GoTest participate in graph restore,
which deliberately excludes imports supplied only by `PackageReference`.

**2. The test snippet** (the only copy-paste; see below for why each piece):

```xml
<ItemGroup Condition="'$(StfTest)' != 'false' AND '$(StfTest)' != '0' AND '$(StfTest)' != 'no' AND '$(StfTest)' != 'n' AND '$(StfTest)' != 'off'">
  <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.11.1" PrivateAssets="all" GeneratePathProperty="true" />
  <PackageReference Include="xunit" Version="2.4.2" PrivateAssets="all" />
  <PackageReference Include="xunit.runner.visualstudio" Version="2.4.3" PrivateAssets="all" />
</ItemGroup>
```

**3. Write `Thing.Test.cs`** next to `Thing.cs` (singular, like Go's `_test.go`;
plural `*.Tests.cs` is also excluded from prod, matching case-insensitively
so lowercase `*.test.cs` strips too). Tests see `internal`s
directly: same assembly, no hacks.

**Exe projects**: keep the entry point in a thin `Program.cs` (see [Exes](#exes)).
No `StfTest` property is needed for the default command behavior.

## Behavior table

`StfTest` defaults to `true` for build/test/IDE evaluations and `false` for
`dotnet pack` and `dotnet publish`. Explicit properties override that default.
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
| `pack` / `publish` (lib or exe) | native SDK targets: **PROD only** | direct PROD |
| `run` (exe) | **runs the prod app** | runs the app |
| `run` (lib) | only executable projects are supported | same |

The `StfAllowTestSlicePublish=true` and `StfAllowTestSlicePack=true` escape hatches
ship the test slice. Test packing includes its DLL, symbols and generated XML
documentation, including multi-framework and `--no-build` packages.
`StfDualBuild=false` disables the dual build
(and the nested prod clean: prod outputs survive `dotnet clean`).
`StfAllowExeTestMode=true` silences the exe guard. `StfAllowPack=false` leaves
`pack` fully alone (no prod redirect: pack behaves like a plain test project).
`StfAllowBareTestRefs=true` silences the unconditioned-ref warning (also
covers `xunit.v3`). In the IDE, `#if STF_TEST` marks test-only code.

`UseArtifactsOutput` keeps working: slices separate under
`artifacts/bin/<project>/` (`test/` + base); nested builds forward the flags.

## Production publishing and validation

`dotnet publish` and `dotnet pack` evaluate production from the start, as if
`-p:StfTest=false` had been supplied. They do not build or publish the test
assembly. Existing `-o`, `PublishDir`, `PackageOutputPath`, configuration,
package version and other command-line properties are preserved.

For example, `dotnet publish -o ./out` writes production to `./out`, and
`dotnet pack -o ./packages -p:PackageVersion=2.3.4` creates the production
package there. `--no-build` uses the existing production build.

Multi-framework `dotnet pack` includes every target framework. For publish,
select one with `-f <framework>`, as with a normal multi-framework project.
The SDK's own restore/build/pack/publish targets execute normally. Consumer
`BeforeTargets`/`AfterTargets` hooks run once with the production properties
and the usual prerequisites. Ordinary applications publishing a
`ProjectReference` to a Stf.GoTest SDK project receive its production assembly.
`GeneratePackageOnBuild=true` also packages production and invokes pack hooks
once, while the default build still produces both assemblies.

`--no-restore` performs no implicit production restore, including solution
builds running on worker nodes. Prepare matching production assets first with
`dotnet restore -p:StfTest=false` (include the same framework/runtime options).
`--no-build` uses an existing production build. Neither option substitutes
for the prerequisites that a normal .NET SDK project requires.

`dotnet build` still produces both assemblies; `dotnet test` executes the test
assembly, and `dotnet run` executes the production application. The
`StfAllowTestSlicePublish=true` and `StfAllowTestSlicePack=true` opt-ins retain
the ability to publish and package tests.

When upgrading, update the test dependency condition to the full snippet above:
older snippets only exclude dependencies for `false`, not `0/no/n/off`.

Run `python tests/regression.py` to build the package and validate temporary
consumers with an isolated NuGet cache. Each dotnet command has a 90-second
limit; CI runs these checks on Linux, Windows and macOS.

## Why the snippet looks like this

Every piece exists because of a verified MSBuild/NuGet wall:

- **The full `StfTest` condition above** — the SDK selects the mode before
  dependency collection. These conditions keep test-framework packages out of
  production restore and accept the same false aliases as compilation.
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
   single pass. This is an explicit prod-only opt-out: omit it to keep the
   default dual build and test discovery. `pack` and `publish` select production
   independently of the executable entry-point layout.

How a real exe is told apart from a library: the SDK sets `UseAppHost` only
when the `<OutputType>Exe` was written by the user (the `Exe` the TestSDK
imposes on libraries arrives too late to trigger it). Libraries, with or
without a `Program.cs`, never notice.

## Legacy PackageReference integration

The package still contains the previous `build` imports for existing consumers.
Their basic build/test/run and production dispatch behavior remains available.
Use the SDK integration above for native pack/publish lifecycle semantics:
legacy dispatch can duplicate shipping hooks, restore despite `--no-restore`,
and return a test assembly to an ordinary referencing application. Existing
legacy consumers can use explicit `-p:StfTest=false` while migrating.

## Known limits

- `WinExe`: the guard fires on all 3 CI OSes (the backstop step prints the
  branch). A degraded restore (no `Pkg`) skips the guard, but the build still
  fails loud with the upstream compiler error. IDE behavior (VS/Rider/VSCode)
  still to verify.
- `dotnet build --no-restore` or `dotnet test --no-restore` right after a
  dual build can fail with missing test references: the dual leaves prod assets in `obj/` and only a restore heals back
  to the test closure. Any command with restore recovers.
- `dotnet test` with `StfTest=false` goes green silently with 0 tests
  (candidate for its own warning).
- `*.Test.cs` that doesn't compile blocks `dotnet run` (the CLI's build phase
  is indistinguishable from `build`); failing asserts don't.
- NativeAOT uses production by default too:
  `dotnet publish -p:PublishAot=true -r <rid>`. CI verifies a runnable native
  binary with no managed DLLs, even with an uncompilable test file present.
- `StfSeparateOutputs=false` shares one output dir for both slices and skips
  the nested prod build (it would clobber the test closure); prod arrives via
  an explicit `-p:StfTest=false` build. Like `-c`, repeat the flag on every
  command (`build` and `test`), it is per-invocation.
- Multi-framework `dotnet publish` still needs `-f` (upstream NETSDK1129),
  exactly like explicit production publication.
