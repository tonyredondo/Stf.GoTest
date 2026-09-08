"""End-to-end package regressions; all consumer projects live in a temporary directory."""
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FALSE_CONDITION = next(group.attrib["Condition"] for group in ET.parse(ROOT / "demo/Demo/Demo.csproj").findall("ItemGroup") if "Condition" in group.attrib)


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="stf-regression-")
        cls.addClassCleanup(cls.temp.cleanup)
        # Match dotnet's physical working directory (macOS /var aliases
        # /private/var); mixed spellings break SDK artifact glob exclusions.
        cls.root = Path(cls.temp.name).resolve()
        cls.env = dict(os.environ, NUGET_PACKAGES=str(cls.root / "cache"),
                       DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
        cls.version = ET.parse(ROOT / "src/Stf.GoTest/Stf.GoTest.csproj").findtext(".//Version")
        result = subprocess.run(["dotnet", "pack", str(ROOT / "src/Stf.GoTest/Stf.GoTest.csproj"),
                                 "-o", str(cls.root / "feed"), "-v:q"],
                                env=cls.env, text=True, capture_output=True, timeout=90)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        config = ET.Element("configuration")
        sources = ET.SubElement(config, "packageSources")
        ET.SubElement(sources, "clear")
        ET.SubElement(sources, "add", key="local", value=str(cls.root / "feed"))
        ET.SubElement(sources, "add", key="nuget.org", value="https://api.nuget.org/v3/index.json")
        ET.ElementTree(config).write(cls.root / "nuget.config", encoding="unicode")

    def project(self, properties="", frameworks="<TargetFramework>net8.0</TargetFramework>", name=None):
        folder = self.root / (name or self._testMethodName)
        folder.mkdir()
        (folder / "Case.csproj").write_text(f'''<Project Sdk="Microsoft.NET.Sdk;Stf.GoTest/{self.version}">
  <PropertyGroup>{frameworks}{properties}</PropertyGroup>
  <ItemGroup Condition="{FALSE_CONDITION}">
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.11.1" PrivateAssets="all" GeneratePathProperty="true" />
    <PackageReference Include="xunit" Version="2.4.2" PrivateAssets="all" />
    <PackageReference Include="xunit.runner.visualstudio" Version="2.4.3" PrivateAssets="all" />
  </ItemGroup>
</Project>''')
        (folder / "Calc.cs").write_text("public static class Calc { public static int Add(int a, int b) => a + b; }")
        (folder / "Calc.Test.cs").write_text("public class Tests { [Xunit.Fact] public void Works() => Xunit.Assert.Equal(3, Calc.Add(1, 2)); }")
        return folder

    def dotnet(self, folder, *args, success=True):
        result = subprocess.run(["dotnet", *args, "-v:minimal"], cwd=folder, env=self.env,
                                text=True, capture_output=True, timeout=90)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode == 0, success, output)
        return output

    def add_targets(self, folder, targets):
        project = folder / "Case.csproj"
        project.write_text(project.read_text().replace("</Project>", targets + "</Project>"))

    def test_nested_warning_obeys_warnaserror(self):
        folder = self.project()
        self.add_targets(folder, '''<Target Name="ProductionWarning" BeforeTargets="BeforeBuild" Condition="'$(StfTest)' == 'false'">
          <Warning Code="STF9001" Text="Production diagnostic" />
        </Target>''')
        self.assertIn("warning STF9001", self.dotnet(folder, "build"))
        self.assertIn("error STF9001", self.dotnet(folder, "build", "-warnaserror", success=False))
        self.dotnet(folder, "build", "-warnaserror", "-warnnotaserror:STF9001")

    def test_global_sdk_extension_keeps_production_publish(self):
        folder = self.project()
        extension = folder / "custom.targets"
        extension.write_text('''<Project><Target Name="CustomSdkHook" AfterTargets="Publish">
          <WriteLinesToFile File="custom-hook.txt" Lines="$(StfTest)" Overwrite="false" />
        </Target></Project>''')
        (folder / "Broken.Test.cs").write_text("#error Production must exclude tests\n")
        self.dotnet(folder, "publish", "-p:AfterMicrosoftNETSdkTargets=" + str(extension))
        self.assert_prod(folder / "bin/Release/net8.0/publish")
        self.assertEqual((folder / "custom-hook.txt").read_text().splitlines(), ["false"])

    def test_shipping_hooks_run_once_after_compilation(self):
        folder = self.project()
        self.add_targets(folder, '''<Target Name="BeforeShipping" BeforeTargets="Pack;Publish">
          <Error Condition="!Exists('$(TargetPath)')" Text="Shipping hook requires the compiled assembly" />
          <WriteLinesToFile File="shipping-hooks.txt" Lines="before:$(StfTest)" Overwrite="false" />
        </Target>
        <Target Name="AfterShipping" AfterTargets="Pack;Publish">
          <WriteLinesToFile File="shipping-hooks.txt" Lines="after:$(StfTest)" Overwrite="false" />
        </Target>''')
        for command in ("publish", "pack"):
            with self.subTest(command=command):
                record = folder / "shipping-hooks.txt"
                record.unlink(missing_ok=True)
                self.dotnet(folder, command)
                self.assertEqual(record.read_text().splitlines(), ["before:false", "after:false"])

    def test_plain_app_publishes_production_project_reference(self):
        library = self.project()
        (library / "Calc.cs").write_text('''public static class Calc {
          public static string Slice =>
        #if STF_TEST
          "TEST";
        #else
          "PROD";
        #endif
          public static int Add(int a, int b) => a + b;
        }''')
        app = self.root / (self._testMethodName + "_app")
        app.mkdir()
        (app / "App.csproj").write_text(f'''<Project Sdk="Microsoft.NET.Sdk">
          <PropertyGroup><TargetFramework>net8.0</TargetFramework><OutputType>Exe</OutputType></PropertyGroup>
          <ItemGroup><ProjectReference Include="../{library.name}/Case.csproj" /></ItemGroup>
        </Project>''')
        (app / "Program.cs").write_text('System.Console.WriteLine(Calc.Slice);')
        self.dotnet(app, "publish")
        published = app / "bin/Release/net8.0/publish"
        result = subprocess.run(["dotnet", str(published / "App.dll")], env=self.env,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "PROD")
        self.assertEqual((published / "Case.dll").read_bytes(),
                         (library / "bin/Release/net8.0/Case.dll").read_bytes())

    def test_shipping_no_restore_does_not_invoke_restore(self):
        folder = self.project()
        self.add_targets(folder, '''<Target Name="CountRestore" BeforeTargets="Restore">
          <WriteLinesToFile File="restore-calls.txt" Lines="restore" Overwrite="false" />
        </Target>''')
        self.dotnet(folder, "restore", "-p:StfTest=false")
        marker = folder / "restore-calls.txt"
        marker.unlink(missing_ok=True)
        for command in ("publish", "pack"):
            with self.subTest(command=command):
                marker.unlink(missing_ok=True)
                self.dotnet(folder, command, "--no-restore")
                self.assertFalse(marker.exists(), "The command unexpectedly executed Restore")
        self.assert_prod(folder / "bin/Release/net8.0/publish")

    def test_no_restore_with_runtime_identifier(self):
        folder = self.project()
        # Query output is a single property value, independent of host OS/CPU.
        rid = self.dotnet(folder, "msbuild", "-getProperty:NETCoreSdkRuntimeIdentifier").strip()
        self.add_targets(folder, '''<Target Name="CountRestore" BeforeTargets="Restore">
          <WriteLinesToFile File="restore-calls.txt" Lines="restore" Overwrite="false" />
        </Target>''')
        self.dotnet(folder, "restore", "-p:StfTest=false", "-r", rid)
        marker = folder / "restore-calls.txt"
        marker.unlink(missing_ok=True)
        self.dotnet(folder, "publish", "-r", rid, "--no-restore")
        self.assertFalse(marker.exists())
        self.assert_prod(folder / "bin/Release/net8.0" / rid / "publish")

    @unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(), "Linux process-tree cancellation probe")
    def test_cancel_stops_nested_descendants(self):
        folder = self.project()
        self.add_targets(folder, '''<Target Name="SlowProduction" BeforeTargets="BeforeBuild" Condition="'$(StfTest)' == 'false'">
          <Exec Command="/bin/sh slow.sh" />
        </Target>''')
        (folder / "slow.sh").write_text("echo $$ > shell.pid\nsleep 60 &\necho $! > child.pid\nwait\n")
        with (folder / "cancel.log").open("w") as log:
            process = subprocess.Popen(["dotnet", "build", "-v:minimal"], cwd=folder, env=self.env,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                deadline = time.monotonic() + 45
                while not (folder / "child.pid").exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue((folder / "child.pid").exists(), (folder / "cancel.log").read_text())
                children = [int((folder / name).read_text()) for name in ("shell.pid", "child.pid")]
                os.kill(process.pid, signal.SIGINT)
                process.wait(timeout=15)
                self.assertNotEqual(process.returncode, 0)
                for pid in children:
                    status = Path(f"/proc/{pid}/stat")
                    self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z",
                                    f"Owned process {pid} survived cancellation")
            finally:
                # This process group belongs exclusively to this test.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)

    def assert_prod(self, folder, assembly="Case"):
        self.assertTrue((folder / (assembly + ".dll")).is_file(), str(folder))
        bad = [str(p) for p in folder.rglob("*")
               if any(word in p.name.lower() for word in ("xunit", "testhost", "testplatform"))]
        self.assertEqual(bad, [])
        deps = json.loads((folder / (assembly + ".deps.json")).read_text())
        self.assertFalse(any("xunit" in key.lower() or "test" in key.lower()
                             for key in deps["libraries"] if not key.startswith("Stf.GoTest/")), deps["libraries"])

    def use_legacy_reference(self, folder):
        project = folder / "Case.csproj"
        project.write_text(project.read_text().replace(
            f'Microsoft.NET.Sdk;Stf.GoTest/{self.version}', 'Microsoft.NET.Sdk').replace(
            '</Project>', f'<ItemGroup><PackageReference Include="Stf.GoTest" Version="{self.version}" PrivateAssets="all" /></ItemGroup></Project>'))

    def test_legacy_reference_retains_basic_command_compatibility(self):
        folder = self.project()
        self.use_legacy_reference(folder)
        self.dotnet(folder, "build")
        self.assert_prod(folder / "bin/Debug/net8.0")
        self.assertIn("Passed!", self.dotnet(folder, "test", "--no-build"))
        self.dotnet(folder, "publish", "-o", str(folder / "published"))
        self.assert_prod(folder / "published")
        self.dotnet(folder, "pack", "-o", str(folder / "packages"))
        self.assertTrue((folder / "packages/Case.1.0.0.nupkg").exists())

    def test_migrate_a_restored_package_reference_to_sdk(self):
        folder = self.project()
        project = folder / "Case.csproj"
        sdk_project = project.read_text()
        self.use_legacy_reference(folder)
        self.dotnet(folder, "restore")
        project.write_text(sdk_project)
        (folder / "Broken.Test.cs").write_text("#error Publishing must not compile tests\n")
        self.dotnet(folder, "publish", "-o", str(folder / "published"))
        self.assert_prod(folder / "published")
        self.assertFalse((folder / "bin/test").exists())

    def test_generate_package_on_build_uses_prod_and_single_hook(self):
        folder = self.project("<GeneratePackageOnBuild>true</GeneratePackageOnBuild>")
        self.add_targets(folder, '''<Target Name="PackageHook" AfterTargets="Pack">
          <WriteLinesToFile File="pack-hooks.txt" Lines="$(StfTest)" Overwrite="false" />
        </Target>''')
        self.dotnet(folder, "build", "-c", "Release")
        self.assertEqual((folder / "pack-hooks.txt").read_text().splitlines(), ["false"])
        self.assert_prod(folder / "bin/Release/net8.0")
        self.assertTrue((folder / "bin/test/Release/net8.0/Case.dll").exists())
        with zipfile.ZipFile(folder / "bin/Release/Case.1.0.0.nupkg") as package:
            self.assertEqual(package.read("lib/net8.0/Case.dll"),
                             (folder / "bin/Release/net8.0/Case.dll").read_bytes())
        self.assertIn("Passed!", self.dotnet(folder, "test", "-c", "Release", "--no-build"))

    def test_multiframework_automatic_pack(self):
        folder = self.project("<GeneratePackageOnBuild>true</GeneratePackageOnBuild>",
                              frameworks="<TargetFrameworks>net8.0;net9.0</TargetFrameworks>")
        self.dotnet(folder, "build", "-c", "Release")
        with zipfile.ZipFile(folder / "bin/Release/Case.1.0.0.nupkg") as package:
            for framework in ("net8.0", "net9.0"):
                self.assertEqual(package.read(f"lib/{framework}/Case.dll"),
                                 (folder / f"bin/Release/{framework}/Case.dll").read_bytes())
                self.assertTrue((folder / f"bin/test/Release/{framework}/Case.dll").exists())

    def test_no_pack_flag_keeps_dual_build(self):
        folder = self.project()
        self.dotnet(folder, "pack", "-p:StfAllowPack=false")
        self.assertFalse(list(folder.rglob("*.nupkg")))
        self.dotnet(folder, "build", "-p:StfAllowPack=false")
        self.assert_prod(folder / "bin/Debug/net8.0")
        self.assertTrue((folder / "bin/test/Debug/net8.0/Case.dll").exists())

    def test_solution_publish_no_restore_in_worker_nodes(self):
        folder = self.project()
        other = self.project(name=self._testMethodName + "_other")
        (other / "Case.csproj").rename(other / "Other.csproj")
        solution = self.root / (self._testMethodName + ".slnx")
        solution.write_text(f'<Solution><Project Path="{folder.name}/Case.csproj"/><Project Path="{other.name}/Other.csproj"/></Solution>')
        for project_folder, project_name in ((folder, "Case"), (other, "Other")):
            path = project_folder / (project_name + ".csproj")
            path.write_text(path.read_text().replace('</Project>', '''<Target Name="ObserveRestore" BeforeTargets="Restore">
              <WriteLinesToFile File="restore.txt" Lines="restored" Overwrite="false" />
            </Target></Project>'''))
        self.dotnet(self.root, "restore", str(solution), "-p:StfTest=false")
        for project_folder in (folder, other):
            (project_folder / "restore.txt").unlink(missing_ok=True)
        self.dotnet(self.root, "publish", str(solution), "--no-restore", "-m:2")
        for project_folder, project_name in ((folder, "Case"), (other, "Other")):
            self.assertFalse((project_folder / "restore.txt").exists())
            self.assert_prod(project_folder / "bin/Release/net8.0/publish", project_name)

    def test_web_sdk_publishes_static_content(self):
        folder = self.project("<OutputType>Exe</OutputType>")
        project = folder / "Case.csproj"
        project.write_text(project.read_text().replace('Microsoft.NET.Sdk;', 'Microsoft.NET.Sdk.Web;'))
        (folder / "Program.cs").write_text('var app = Microsoft.AspNetCore.Builder.WebApplication.CreateBuilder(args).Build(); app.Run();')
        (folder / "wwwroot").mkdir()
        (folder / "wwwroot/probe.txt").write_text("static content")
        self.dotnet(folder, "publish", "-o", str(folder / "published"))
        self.assert_prod(folder / "published")
        self.assertEqual((folder / "published/wwwroot/probe.txt").read_text(), "static content")

    def test_sdk_test_shipping_opt_in_alias(self):
        folder = self.project()
        self.dotnet(folder, "publish", "-p:StfTest=true", "-p:StfAllowTestSlicePublish=yes", "-o", str(folder / "published"))
        self.assertEqual((folder / "published/Case.dll").read_bytes(),
                         (folder / "bin/test/Release/net8.0/Case.dll").read_bytes())

    def test_sdk_pack_test_opt_in(self):
        folder = self.project()
        self.dotnet(folder, "pack", "-p:StfTest=true", "-p:StfAllowTestSlicePack=on", "-o", str(folder / "packages"))
        with zipfile.ZipFile(folder / "packages/Case.1.0.0.nupkg") as package:
            self.assertEqual(package.read("lib/net8.0/Case.dll"),
                             (folder / "bin/test/Release/net8.0/Case.dll").read_bytes())

    def test_body_explicit_test_shipping(self):
        for command, option in (("pack", "StfAllowTestSlicePack"),
                                ("publish", "StfAllowTestSlicePublish")):
            with self.subTest(command=command):
                folder = self.project(f"<StfTest>true</StfTest><{option}>true</{option}>",
                                      name=self._testMethodName + command)
                (folder / "Calc.Test.cs").write_text(
                    '#if !STF_TEST\n#error Test mode requires STF_TEST\n#endif\n'
                    'public class Tests { [Xunit.Fact] public void Works() {} }')
                self.add_targets(folder, '''<Target Name="BeforeShipping" BeforeTargets="Pack;Publish">
                  <Error Condition="!Exists('$(TargetPath)')" Text="Missing compiled test assembly" />
                  <WriteLinesToFile File="hooks.txt" Lines="before:$(StfTest)" Overwrite="false" />
                </Target><Target Name="AfterShipping" AfterTargets="Pack;Publish">
                  <WriteLinesToFile File="hooks.txt" Lines="after:$(StfTest)" Overwrite="false" />
                </Target>''')
                output = folder / "shipped"
                self.dotnet(folder, command, "-o", str(output))
                self.assertEqual((folder / "hooks.txt").read_text().splitlines(),
                                 ["before:true", "after:true"])
                test_binary = (folder / "bin/test/Release/net8.0/Case.dll").read_bytes()
                if command == "pack":
                    with zipfile.ZipFile(output / "Case.1.0.0.nupkg") as package:
                        self.assertEqual(package.read("lib/net8.0/Case.dll"), test_binary)
                else:
                    self.assertEqual((output / "Case.dll").read_bytes(), test_binary)
                self.assertIn("Passed!", self.dotnet(folder, "test", "-c", "Release", "--no-build"))

    def test_shipping_permission_alone_keeps_production(self):
        for command, option in (("pack", "StfAllowTestSlicePack"),
                                ("publish", "StfAllowTestSlicePublish")):
            for location in ("body", "global"):
                with self.subTest(command=command, location=location):
                    folder = self.project(f"<{option}>on</{option}>" if location == "body" else "",
                                          name=self._testMethodName + command + location)
                    (folder / "Broken.Test.cs").write_text("#error Permission must not select tests\n")
                    args = [f"-p:{option}=on"] if location == "global" else []
                    self.dotnet(folder, command, *args)
                    self.assertFalse((folder / "bin/test").exists())
                    if command == "publish":
                        self.assert_prod(folder / "bin/Release/net8.0/publish")
                    else:
                        with zipfile.ZipFile(folder / "bin/Release/Case.1.0.0.nupkg") as package:
                            self.assertEqual(package.read("lib/net8.0/Case.dll"),
                                             (folder / "bin/Release/net8.0/Case.dll").read_bytes())

    def test_test_shipping_requires_matching_permission(self):
        for command, other in (("pack", "StfAllowTestSlicePublish"),
                               ("publish", "StfAllowTestSlicePack")):
            with self.subTest(command=command):
                folder = self.project(f"<StfTest>true</StfTest><{other}>true</{other}>",
                                      name=self._testMethodName + command)
                output = folder / "shipped"
                log = self.dotnet(folder, command, "-o", str(output), success=False)
                self.assertIn("STF0010" if command == "pack" else "STF0011", log)
                self.assertFalse(list(output.glob("*")))
                # NoBuild must enforce permission too, without compiling anything.
                (folder / "Broken.Test.cs").write_text("#error NoBuild must not compile\n")
                log = self.dotnet(folder, command, "--no-build", "-o", str(output), success=False)
                self.assertIn("STF0010" if command == "pack" else "STF0011", log)
                self.assertNotIn("NoBuild must not compile", log)
                self.assertFalse(list(output.glob("*")))

    def test_explicit_body_production_overrides_shipping_opt_in(self):
        for command, option in (("pack", "StfAllowTestSlicePack"),
                                ("publish", "StfAllowTestSlicePublish")):
            with self.subTest(command=command):
                folder = self.project(f"<StfTest>false</StfTest><{option}>true</{option}>",
                                      name=self._testMethodName + command)
                (folder / "Broken.Test.cs").write_text("#error Explicit production must exclude tests\n")
                output = folder / "shipped"
                self.dotnet(folder, command, "-o", str(output))
                self.assertFalse((folder / "bin/test").exists())
                if command == "pack":
                    with zipfile.ZipFile(output / "Case.1.0.0.nupkg") as package:
                        self.assertEqual(package.read("lib/net8.0/Case.dll"),
                                         (folder / "bin/Release/net8.0/Case.dll").read_bytes())
                else:
                    self.assert_prod(output)

    def test_explicit_test_selection_applies_conditional_properties(self):
        for command, option in (("pack", "StfAllowTestSlicePack"),
                                ("publish", "StfAllowTestSlicePublish")):
            with self.subTest(command=command):
                folder = self.project(name=self._testMethodName + command)
                intent = "_IsPacking" if command == "pack" else "_IsPublishing"
                project = folder / "Case.csproj"
                project.write_text(project.read_text().replace("</PropertyGroup>", f'''</PropertyGroup>
                  <PropertyGroup Condition="'$({intent})' == 'true'">
                    <StfTest>true</StfTest><{option}>true</{option}>
                  </PropertyGroup>
                  <Import Project="test-config.props" Condition="'$(StfTest)' == 'true'" />
                  <PropertyGroup Condition="'$(StfTest)' == 'true'">
                    <DefineConstants>$(DefineConstants);$(TestConfiguration)</DefineConstants>
                  </PropertyGroup>''', 1))
                (folder / "test-config.props").write_text(
                    '<Project><PropertyGroup><TestConfiguration>TEST_CONFIGURATION</TestConfiguration>'
                    '</PropertyGroup></Project>')
                (folder / "Calc.Test.cs").write_text(
                    '#if !TEST_CONFIGURATION\n#error Test configuration must be evaluated\n#endif\n'
                    'public class Tests { [Xunit.Fact] public void Works() {} }')
                self.dotnet(folder, command)
                # The README example changes only the requested shipping command.
                self.dotnet(folder, "publish" if command == "pack" else "pack")
                self.assert_prod(folder / "bin/Release/net8.0")

    def test_body_no_pack_preserves_test_project(self):
        folder = self.project("<StfAllowPack>false</StfAllowPack>")
        self.dotnet(folder, "pack")
        self.assertFalse(list(folder.rglob("*.nupkg")))
        self.dotnet(folder, "build")
        self.assert_prod(folder / "bin/Debug/net8.0")
        self.assertIn("Passed!", self.dotnet(folder, "test", "--no-build"))

    def test_body_shipping_aliases_and_global_override(self):
        folder = self.project("<StfTest>true</StfTest><StfAllowTestSlicePack>on</StfAllowTestSlicePack>"
                              "<StfAllowTestSlicePublish>yes</StfAllowTestSlicePublish>")
        self.add_targets(folder, '''<Target Name="ObserveMode" BeforeTargets="BeforeBuild">
          <WriteLinesToFile File="modes.txt" Lines="$(StfTest)" Overwrite="false" />
        </Target>''')
        for command in ("pack", "publish"):
            self.dotnet(folder, command)
            self.assertIn("true", (folder / "modes.txt").read_text().splitlines())
            (folder / "modes.txt").unlink()
            self.dotnet(folder, command, "-p:StfTest=false")
            self.assertEqual((folder / "modes.txt").read_text().splitlines(), ["false"])
            (folder / "modes.txt").unlink()

    def test_body_shipping_no_restore_keeps_test_assets(self):
        folder = self.project("<StfTest>true</StfTest><StfAllowTestSlicePack>true</StfAllowTestSlicePack>"
                              "<StfAllowTestSlicePublish>true</StfAllowTestSlicePublish>"
                              "<StfDualBuild>false</StfDualBuild>")
        self.add_targets(folder, '''<Target Name="ObserveRestore" BeforeTargets="Restore">
          <WriteLinesToFile File="restored.txt" Lines="$(StfTest)" Overwrite="false" />
        </Target>''')
        self.dotnet(folder, "restore")
        (folder / "restored.txt").unlink()
        for command in ("pack", "publish"):
            self.dotnet(folder, command, "--no-restore")
            self.assertFalse((folder / "restored.txt").exists())
        self.assertIn("Passed!", self.dotnet(folder, "test", "--no-build", "-c", "Release"))

    def test_sdk_pack_test_symbols_docs_and_no_build(self):
        folder = self.project(
            "<GenerateDocumentationFile>true</GenerateDocumentationFile>"
            "<IncludeSymbols>true</IncludeSymbols><SymbolPackageFormat>snupkg</SymbolPackageFormat>",
            frameworks="<TargetFrameworks>net8.0;net9.0</TargetFrameworks>")
        # Configure the intermediate base early, as required by the .NET SDK.
        (folder / "Directory.Build.props").write_text(
            '<Project><PropertyGroup><BaseIntermediateOutputPath>custom obj/</BaseIntermediateOutputPath>'
            '</PropertyGroup></Project>')
        (folder / "Calc.Test.cs").write_text(
            '/// <summary>Test-only documentation.</summary>\n'
            'public class Tests { [Xunit.Fact] public void Works() => Xunit.Assert.Equal(3, Calc.Add(1, 2)); }')
        self.dotnet(folder, "pack", "-p:StfTest=true", "-p:StfAllowTestSlicePack=true", "-o", str(folder / "packages"))
        expected = {}
        for framework in ("net8.0", "net9.0"):
            for extension in ("dll", "pdb", "xml"):
                expected[framework, extension] = (
                    folder / f"custom obj/test/Release/{framework}/Case.{extension}").read_bytes()
            self.assertIn(b"Test-only documentation", expected[framework, "xml"])
        # A second pack must use the existing artifacts, without compiling.
        (folder / "Broken.Test.cs").write_text("#error NoBuild must not compile\n")
        self.dotnet(folder, "pack", "--no-build", "-p:StfTest=true", "-p:StfAllowTestSlicePack=true",
                    "-o", str(folder / "no-build-packages"))
        for output in ("packages", "no-build-packages"):
            with zipfile.ZipFile(folder / output / "Case.1.0.0.nupkg") as package, \
                    zipfile.ZipFile(folder / output / "Case.1.0.0.snupkg") as symbols:
                for (framework, extension), content in expected.items():
                    archive = symbols if extension == "pdb" else package
                    self.assertEqual(archive.read(f"lib/{framework}/Case.{extension}"), content)

    def test_explicit_publish_outputs_prod(self):
        folder = self.project()
        output = folder / "published output"
        self.dotnet(folder, "publish", "-o", str(output))
        self.assert_prod(output)

    def test_publish_without_dual_restores_prod(self):
        folder = self.project()
        self.dotnet(folder, "publish", "-p:StfDualBuild=false")
        self.assert_prod(folder / "bin/Release/net8.0/publish")

    def test_publish_second_framework(self):
        folder = self.project(frameworks="<TargetFrameworks>net8.0;net9.0</TargetFrameworks>")
        self.dotnet(folder, "publish", "-f", "net9.0")
        self.assert_prod(folder / "bin/Release/net9.0/publish")

    def test_dual_preserves_global_properties(self):
        folder = self.project()
        (folder / "Flag.cs").write_text("#if REQUIRED_FLAG && SECOND_FLAG\npublic class Flag {}\n#else\n#error Missing REQUIRED_FLAG\n#endif\n")
        self.dotnet(folder, "build", "-p:DefineConstants=REQUIRED_FLAG%3BSECOND_FLAG", "-c", "With Space", "-p:Platform=x64")
        self.assert_prod(folder / "bin/x64/With Space/net8.0")
        self.assertTrue((folder / "bin/test/x64/With Space/net8.0/Case.dll").exists())

    def test_false_aliases_restore_only_prod(self):
        folder = self.project()
        for value in ("0", "no", "n", "off", "false"):
            with self.subTest(value=value):
                self.dotnet(folder, "build", f"-p:StfTest={value}")
                self.assert_prod(folder / "bin/Debug/net8.0")
                deps = json.loads((folder / "obj/project.assets.json").read_text())["libraries"]
                self.assertEqual(list(deps), [])

    def test_body_shared_output_runs_tests(self):
        folder = self.project("<StfSeparateOutputs>false</StfSeparateOutputs>")
        self.dotnet(folder, "build")
        self.assertTrue((folder / "bin/Debug/net8.0/Case.dll").exists())
        self.assertFalse((folder / "bin/test").exists())
        self.assertIn("Passed!", self.dotnet(folder, "test", "--no-build"))

    def test_default_build_test_clean_and_pack(self):
        folder = self.project()
        self.assertIn("Passed!", self.dotnet(folder, "test"))
        self.assert_prod(folder / "bin/Debug/net8.0")
        self.assertIn("Passed!", self.dotnet(folder, "test", "--no-build"))
        self.dotnet(folder, "clean")
        self.assertFalse((folder / "bin/Debug/net8.0/Case.dll").exists())
        self.assertFalse((folder / "bin/test/Debug/net8.0/Case.dll").exists())
        self.dotnet(folder, "pack", "-p:StfDualBuild=false")
        with zipfile.ZipFile(folder / "bin/Release/Case.1.0.0.nupkg") as package:
            self.assertEqual(package.read("lib/net8.0/Case.dll"),
                             (folder / "bin/Release/net8.0/Case.dll").read_bytes())

    def test_exe_run_preserves_globals(self):
        folder = self.project("<OutputType>Exe</OutputType>")
        (folder / "Program.cs").write_text('public static class Program { public static void Main() => System.Console.WriteLine(Calc.Add(1, 2)); }')
        (folder / "Flag.cs").write_text("#if REQUIRED_FLAG\npublic class Flag {}\n#else\n#error Missing REQUIRED_FLAG\n#endif\n")
        self.assertTrue(self.dotnet(folder, "run", "-p:DefineConstants=REQUIRED_FLAG").strip().endswith("3"))
        self.assert_prod(folder / "bin/Debug/net8.0")

    def test_multiframework_dual_and_artifacts(self):
        folder = self.project(frameworks="<TargetFrameworks>net8.0;net9.0</TargetFrameworks>")
        artifacts = folder / "artifacts with spaces"
        args = ("-p:UseArtifactsOutput=true", "-p:ArtifactsPath=" + str(artifacts))
        self.dotnet(folder, "build", *args)
        for framework in ("net8.0", "net9.0"):
            self.assert_prod(artifacts / ("bin/Case/debug_" + framework))
            self.assertTrue((artifacts / ("bin/Case/test/debug_" + framework) / "Case.dll").exists())
        self.dotnet(folder, "clean", *args)
        self.assertFalse(list((artifacts / "bin").rglob("Case.dll")))

    def test_publish_test_opt_in(self):
        folder = self.project()
        output = folder / "intentional tests"
        self.dotnet(folder, "publish", "-p:StfTest=true", "-p:StfAllowTestSlicePublish=true", "-o", str(output))
        self.assertEqual((output / "Case.dll").read_bytes(),
                         (folder / "bin/test/Release/net8.0/Case.dll").read_bytes())

    def test_nested_build_failure_is_not_swallowed(self):
        folder = self.project()
        (folder / "ProdFailure.cs").write_text("#if !STF_TEST\n#error Production compilation failed deliberately\n#endif\n")
        log = self.dotnet(folder, "build", success=False)
        self.assertIn("nested Build failed", log)
        self.assertIn("Production compilation failed deliberately", log)
        self.assertFalse((folder / "bin/Debug/net8.0/Case.dll").exists())

    def test_no_build_publish_outputs_prod(self):
        folder = self.project()
        self.dotnet(folder, "build", "-c", "Release")
        output = folder / "no build output"
        self.dotnet(folder, "publish", "--no-build", "-o", str(output))
        self.assert_prod(output)

    def test_pack_explicit_output_and_version(self):
        folder = self.project()
        output = folder / "packages with spaces"
        self.dotnet(folder, "pack", "-o", str(output), "-p:PackageVersion=2.3.4")
        with zipfile.ZipFile(output / "Case.2.3.4.nupkg") as package:
            self.assertEqual(package.read("lib/net8.0/Case.dll"),
                             (folder / "bin/Release/net8.0/Case.dll").read_bytes())
        self.assertFalse((folder / "bin/test").exists())

    def test_pack_multiple_frameworks(self):
        folder = self.project(frameworks="<TargetFrameworks>net8.0;net9.0</TargetFrameworks>")
        output = folder / "packages"
        self.dotnet(folder, "pack", "-o", str(output))
        with zipfile.ZipFile(output / "Case.1.0.0.nupkg") as package:
            for framework in ("net8.0", "net9.0"):
                self.assertEqual(package.read("lib/" + framework + "/Case.dll"),
                                 (folder / "bin/Release" / framework / "Case.dll").read_bytes())

    def test_shipping_does_not_compile_tests(self):
        folder = self.project()
        (folder / "Broken.Test.cs").write_text("#error Tests must not compile during production commands\n")
        self.dotnet(folder, "publish", "-o", str(folder / "published"))
        self.assert_prod(folder / "published")
        self.dotnet(folder, "pack", "-o", str(folder / "packages"))
        self.assertTrue((folder / "packages/Case.1.0.0.nupkg").exists())

    def test_command_contract_with_failing_assertion(self):
        folder = self.project("<OutputType>Exe</OutputType>")
        (folder / "Program.cs").write_text('public static class Program { public static void Main() => System.Console.WriteLine("PROD APP"); }')
        (folder / "Calc.Test.cs").write_text('public class Tests { [Xunit.Fact] public void Fails() => Xunit.Assert.True(false, "TEST ASSEMBLY EXECUTED"); }')
        self.dotnet(folder, "build")
        self.assertTrue((folder / "bin/test/Debug/net8.0/Case.dll").exists())
        self.assert_prod(folder / "bin/Debug/net8.0")
        self.assertTrue(self.dotnet(folder, "run").strip().endswith("PROD APP"))
        self.assertIn("TEST ASSEMBLY EXECUTED", self.dotnet(folder, "test", "--no-build", success=False))
        self.dotnet(folder, "publish", "-o", str(folder / "published"))
        self.assert_prod(folder / "published")
        self.dotnet(folder, "pack", "-o", str(folder / "packages"))
        self.assertTrue((folder / "packages/Case.1.0.0.nupkg").exists())

    def test_no_build_pack_preserves_production_binary(self):
        folder = self.project()
        self.dotnet(folder, "build", "-c", "Release")
        binary = (folder / "bin/Release/net8.0/Case.dll").read_bytes()
        (folder / "Calc.cs").write_text("#error No compilation was requested\n")
        output = folder / "packages"
        self.dotnet(folder, "pack", "--no-build", "-o", str(output))
        with zipfile.ZipFile(output / "Case.1.0.0.nupkg") as package:
            self.assertEqual(package.read("lib/net8.0/Case.dll"), binary)

    def test_global_output_properties(self):
        folder = self.project()
        output = folder / "global publish"
        packages = folder / "global packages"
        self.dotnet(folder, "publish", "-p:PublishDir=" + str(output))
        self.assert_prod(output)
        self.dotnet(folder, "pack", "-p:PackageOutputPath=" + str(packages))
        self.assertTrue((packages / "Case.1.0.0.nupkg").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
