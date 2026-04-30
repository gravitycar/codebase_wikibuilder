# Codebase Wiki Builder

**overview**
The Codebase Wiki Builder is an application that will scan the codebase of another application (the "target codebase") and create summary files for each file in the target codebase. These summary files will be written in markdown format and placed in an Obsidian vault. From there, the summary files will be searchable and readable. They can be used as quick reference material to give an LLM insight into an application without having to scan every file.

## Purpose
The purpose is to give LLM coding agents a much more compact way to understand a complete codebase than scanning the entire codebase, over and over again. These summaries should give an LLM enough information to know how to build new features or fix existing bugs using established patterns and features instead of inventing new code to do the same things the exsting code currently does.

## Inspiration
This Gist is the original inspiration for this project:
https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

Karpathy's idea is that the user and/or the LLM would copy web pages, podcasts, PDF's and other documents into a local "sources" directory and then have the LLM currate wiki pages based on those sources. This approach is slightly different in that instead of pulling down content from the internet, it will restrict itself to generating summaries of files in an application's codebase.

## Process
1) Scan the target codebase directory for directories and files.
2) For each directory in the target codebase, create a copy of that directory (the "target directory") in the Obsidian vault if it doesn't already exist.
3) For each file in the target codebase (the "target file"), get an MD5 hash of the target file's contents. 
4) See if there is already a corresponding summary file for the target file in the vault. If there is, read the summary file for the MD5 hash entry. If the summary file's MD5 hash entry is the same as the current MD5 hash for the target file, then the target file is already summarized and the summary is up-to-date. No futher action is necessary for this target file. Otherwise, proceed.
5) Read the target file's contents and send them to OpenAI for summarization.
6) When the file is summarized, get an MD5 hash for the file.
7) Search the target codebase for any references to the file. Make a list of all references, explicit and dynamic.
8) Create the summary file in the corresponding directory in the Obsidian vault. If the target file defines a class or a module, then there should be summary of the class/module and brief summaries of each property and method the class/module defines. Otherwise, a simple description of what the file does should be sufficient. Add a list of every file in the target codebase that references the target file to the bottom of the summary as backlinks. Finally, add the MD5 hash so we can tell when the target file has been updated, and we need to regenerate the summary.
9) Once all the summaries are created, we can perform analysis of the summary files and produce an overview of the application, its primary purpose and dominant patterns. 

Once all of that is done, the wiki should be ready to be searched and read by an LLM that wants to write new code in the target codebase.

## Interface
This application will be a CLI application, and will execute on the user's local system.
It should define simple commands to perform the following operations:
- **Ingest:** Scan the target codebase and write the summary files as described above.
- **Analysis:** Review the summaries and look for larger takeaways. Do the summaries show a consistent use of any well known software engineering patterns or practices? What does the target application as a whole appear to be intended to do? Does it actually do that? Are their obvous flaws or inconsistencies in the target application? This analysis should be written to an `overview.md` file in the root directory of the obsidian vault.
- **Query:** Pose a question to the LLM and tell it to base its answers on the data in the wiki.

## Indexing
Per Karpathy's Gist, we should generate an `index.md` file that will be a catalog of everything in the wiki. Each wiki page should be listed with a link and a one-line description. Queries against the wiki should start with the `index.md` file and move on from there.

## Logging
This application will keep two kinds of logs:
**`log.md`**: Per Karpathy's Gist, keeping a log of updates will be useful for tracking the evolution of the target application as it grows. We will append a log entry for every ingest, query and analysis. Log entries will start with a date/time stamp in YYYY-MM-DD H:m:s.  
**`logs/<current_date>_<current_time>.log`**: this will be a stardard log file to record the normal operations of the application and any error conditions it encounters to assist with debugging.

## Security
Since this is a local, single-user application, we won't be very concerned with security issues like prompt-injection or source-poisoning, as bad actors are unlikely to ever access this application. However, secrets like API keys or user/passwords will be stored in a `.env` file and will not be hard-coded in the application code itself.

## Leveraging Obsidian
**backlinks**: Obsidian supports `backlinks`, which allow one markdown file to be linked to another markdown file, just like hyperlinks in HTML. The summary files this application generates should make use of backlinks whenever possible.
**Command Line Interface**: Obsidian supports a command line interface that claims "Anything you can do in Obsidian you can do from the command line.". Our application needs to leverage that feature. The source documentation for Obsidian's command line featue is here: `https://obsidian.md/help/cli`. 
**Search**: We will want to search through the Obsidian vault. Obsidian suppports a search feature as a core plugin. Here is the documentation: `https://obsidian.md/help/plugins/search`. 

When our application starts, the Obsidian vault may not have the search plugin enabled. We should have the ability to update the enabled plugins programmatically. This command will list all available core plugins:
```
plugins filter=core versions format = json
```

The results from the `plugins` command should include a plugin id for every plugin. We can parse those results for the `search` plugin (or any other plugin) to get its id. 

This command will enable a plugin:
```
plugin:enable id=<plugin_id>
```

## Configuration
We shouldn't have too many configuration variables to contend with here. 
- **OpenAI API KEY** - this is secret and needs to be kept in a `.env` file, which we'll read at runtime.
- **OpenAI URL** - this can also go in `.env`.
- **Target Codebase** - a local absolute path to the code we want to build our wiki from.
- **Obsidian Vault** - a local absolute path to the obsidian vault directory.

The `Target Codebase` and `Obsidian Vault` variables need to come in pairs. If we have multiple applications we want to build codebase wiki's for, we need to make sure that the wiki files for "Codbase A" go in the wiki for "Codebase A Obsidian Vault" and not in "Codebase B Obsidian Vault". 

Maybe what we need is to only run this tool from the root of the obsidian vault. Then we at least can't use the wrong vault! But we would also need something in the vault that specifies the absolute path to the target codebase. On initial setup/first run, the application would have to ask the user to provide the absolute path to the target codebase, and then record that path somewhere, like the `index.md` file perhaps, so that subsequent runs of this application look at the same codebase.


## Deleted files
If a file in the target codebase was previously summarized in our obsidian vault, and then deleted from the target codebase, the summary file should be deleted as well. And any files that contain backlinks to the deleted summary file should have those backlinks removed. 