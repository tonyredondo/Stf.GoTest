using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using Microsoft.Build.Framework;
using Microsoft.Build.Utilities;

// A separate process is necessary: MSBuild caches imported NuGet XML even
// across project evaluations after Restore rewrites those imports.
public sealed class StfRunProd : Task, ICancelableTask
{
    [Required] public string Project { get; set; }
    [Required] public string Target { get; set; }
    public bool AllFrameworks { get; set; }
    public bool Restore { get; set; }
    public string GetProperty { get; set; }
    [Output] public string PropertyValue { get; set; }
    private readonly object gate = new object();
    private Process running;
    private bool canceled;

    public override bool Execute()
    {
        var properties = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var property in ((IBuildEngine6)BuildEngine).GetGlobalProperties())
            properties[property.Key] = property.Value;
        properties["StfTest"] = "false";
        properties["StfNested"] = "true";
        if (AllFrameworks) properties.Remove("TargetFramework");
        if (Restore)
        {
            var restoreProperties = new Dictionary<string, string>(properties, StringComparer.OrdinalIgnoreCase);
            restoreProperties.Remove("TargetFramework");
            if (!Run(restoreProperties, "Restore", null)) return false;
        }
        return Run(properties, Target, GetProperty);
    }

    private bool Run(Dictionary<string, string> properties, string target, string query)
    {
        var start = new ProcessStartInfo("dotnet") {
            WorkingDirectory = Path.GetDirectoryName(Project),
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        var arguments = new CommandLineBuilder();
        arguments.AppendSwitch("msbuild");
        arguments.AppendFileNameIfNotNull(Project);
        arguments.AppendSwitch("-nologo");
        if (string.IsNullOrEmpty(query))
        {
            arguments.AppendSwitchIfNotNull("-t:", target);
            arguments.AppendSwitch("-v:minimal");
            arguments.AppendSwitch("-consoleloggerparameters:NoSummary");
        }
        else arguments.AppendSwitchIfNotNull("-getProperty:", query);
        foreach (var property in properties)
        {
            // GetGlobalProperties preserves MSBuild percent escapes. Do not
            // escape percent again: %3B must still become a semicolon.
            // CommandLineBuilder quotes the argument without invoking a shell.
            var value = property.Value;
            foreach (char c in new[] { ';', ',', '"', '\r', '\n' })
                value = value.Replace(c.ToString(), "%" + ((int)c).ToString("X2"));
            arguments.AppendSwitchIfNotNull("-p:" + property.Key + "=", value);
        }
        start.Arguments = arguments.ToString();
        using (var process = new Process { StartInfo = start })
        {
            lock (gate)
            {
                if (canceled) return false;
                process.Start();
                running = process;
            }
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            process.WaitForExit();
            lock (gate) running = null;
            var output = stdout.GetAwaiter().GetResult();
            var error = stderr.GetAwaiter().GetResult();
            if (string.IsNullOrEmpty(query)) LogOutput(output);
            else PropertyValue = output.Trim();
            LogOutput(error);
            if (process.ExitCode != 0)
            {
                Log.LogError("Stf.GoTest: nested {0} failed (exit {1}).", target, process.ExitCode);
                return false;
            }
            return true;
        }
    }

    private void LogOutput(string output)
    {
        // Preserve MSBuild diagnostic events, including codes and locations.
        // The parent engine then applies -warnaserror/-warnasmessage normally.
        using (var reader = new StringReader(output))
        {
            string line;
            while ((line = reader.ReadLine()) != null)
                Log.LogMessageFromText(line, MessageImportance.High);
        }
    }

    public void Cancel()
    {
        lock (gate)
        {
            canceled = true;
            if (running != null && !running.HasExited)
            {
                // RoslynCodeTaskFactory compiles against netstandard2.0, whose
                // reference assembly lacks Kill(bool). All supported dotnet
                // hosts implement it; invoke the runtime API to kill children too.
                var killTree = typeof(Process).GetMethod("Kill", new[] { typeof(bool) });
                try { killTree.Invoke(running, new object[] { true }); }
                catch (System.Reflection.TargetInvocationException ex)
                {
                    if (!(ex.InnerException is InvalidOperationException)) throw;
                }
            }
        }
    }
}
