return {
    -- Latest Blink requires Neovim 0.12+ and blink.lib.
    'saghen/blink.cmp',
    dependencies = { 'saghen/blink.lib' },
    build = function()
        require('blink.cmp').build():pwait()
    end,
    config = function()
        require('config.blink')
    end,
}
