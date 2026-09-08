using Xunit;

namespace Demo;

public class CalculatorTests
{
    [Fact]
    public void Add_is_visible_without_InternalsVisibleTo()
    {
        Assert.Equal(3, new Calculator().Add(1, 2));
    }

    [Fact]
    public void Test_build_defines_STF_TEST()
    {
#if STF_TEST
        Assert.Equal("TEST", Calculator.BuildMode);
#else
        Assert.Fail("STF_TEST should be defined in test builds");
#endif
    }
}
