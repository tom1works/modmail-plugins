# API Modmail Plugin (persistent configuration)

Settings are saved automatically to `config.json` in this plugin folder when using `?api setgroup`, `?api setrole`, `?api setminrank`, `?api setmaxrank`, or `?api setkey`. The plugin loads that file on startup, so you do not need a `.env` file for these settings.

## Commands

- `?api setup`
- `?api config`
- `?api setgroup <Roblox group ID>`
- `?api setrole <Discord role ID|@role>`
- `?api setminrank <rank>`
- `?api setmaxrank <rank>`
- `?api setkey <API key>`
- `?api test`
- `?search <Roblox username or ID>`
- `?rank <username or ID> <rank name or number> | <reason>`
- `?ranks` (alias `?apiranks`)

## Install

1. Copy `api_modmail` into your Modmail repository's `plugins` directory.
2. Ensure the process can write to that plugin folder and that the folder is on persistent storage.
3. Load/reload the plugin.
4. Run the `?api` setup commands in a private staff channel.

The API endpoint defaults to `https://api.rankgun.works`. To override it without `.env`, edit `api_url` in the generated `config.json`. The config file is created after the first setting change. You can also optionally set `API_MODMAIL_CONFIG_PATH` in the hosting environment if you need the config file in a persistent data directory.

## Security and persistence

- The API key is stored in plaintext in `config.json`. The plugin attempts to restrict file permissions to the owner on platforms that support it. Limit filesystem access, do not commit this file to source control, and back it up securely.
- Do not use `?api setkey` in a public channel because Discord may retain the command message containing the key. Use a private staff channel and delete the message after.
- A local JSON file persists across bot restarts only if the host preserves the plugin folder. Ephemeral hosting/container filesystems may be wiped on redeploy/restart; in that case set `API_MODMAIL_CONFIG_PATH` to a persistent mounted directory or use a database.
- Existing environment variables remain optional fallback defaults. Saved values in `config.json` override them.
- `?api test` checks key presence, Roblox group role lookup, and API host reachability; it does not perform a live rank change.
