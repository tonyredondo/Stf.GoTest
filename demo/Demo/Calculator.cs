namespace Demo;

public class Calculator
{
    // internal WITHOUT InternalsVisibleTo: visible to tests because
    // test build compiles the same assembly plus *.Test.cs.
    internal int Add(int a, int b) => a + b;

#if STF_TEST
    internal static string BuildMode => "TEST";
#else
    internal static string BuildMode => "PROD";
#endif
}
