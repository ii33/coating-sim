// Small per-user installer stub. The compressed ZIP payload and its length
// footer are appended by build_offline_installer.py; no external extractor is
// required on the target computer.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Text;
using System.Windows.Forms;

internal static class OfflineSetup
{
    private const string Footer = "CSOFSET1";
    private const int FooterLength = 16;
    private const string MarkerName = ".coatsim-install-marker";
    private const string ProgressName = ".coatsim-install-in-progress";
    private const string MarkerContent = "CoatSim-Offline-Installer-v1";
    private const string UserDataMarkerContent = "CoatSim-User-Data-Preserved-v1";

    [STAThread]
    private static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string executableName = Path.GetFileNameWithoutExtension(Application.ExecutablePath);
        if (args.Length == 2 && String.Equals(args[0], "--install-to", StringComparison.OrdinalIgnoreCase))
            return InstallTo(args[1], true);
        if ((args.Length >= 1 && String.Equals(args[0], "--uninstall", StringComparison.OrdinalIgnoreCase))
            || String.Equals(executableName, "Uninstall-CoatSim", StringComparison.OrdinalIgnoreCase))
            return Uninstall(args.Length >= 2 ? args[1] : Path.GetDirectoryName(Application.ExecutablePath), args.Length >= 1 && String.Equals(args[0], "--uninstall", StringComparison.OrdinalIgnoreCase));
        Application.Run(new SetupForm());
        return 0;
    }

    private static int InstallTo(string target, bool silent)
    {
        try
        {
            BackgroundWorker reporter = new BackgroundWorker();
            reporter.WorkerReportsProgress = true;
            ExtractPayload(Path.GetFullPath(target), reporter);
            return 0;
        }
        catch (Exception ex)
        {
            if (!silent) MessageBox.Show(ex.Message, "CoatSim installation failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private static int Uninstall(string target, bool silent)
    {
        try
        {
            string root = Path.GetFullPath(target).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            if (root.Length < 4 || !Directory.Exists(root)) throw new IOException("The installation folder does not exist.");
            DirectoryInfo rootInfo = new DirectoryInfo(root);
            if ((rootInfo.Attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("The installation folder is a link or junction.");
            string marker = Path.Combine(root, MarkerName);
            if (!File.Exists(marker)) throw new IOException("The CoatSim install marker was not found. No files were removed.");
            string markerText = File.ReadAllText(marker, Encoding.UTF8).Trim();
            if (markerText != MarkerContent && markerText != UserDataMarkerContent)
                throw new IOException("The CoatSim install marker is not recognized. No files were removed.");

            foreach (Process process in Process.GetProcessesByName("CoatSim"))
            {
                try
                {
                    string executable = process.MainModule.FileName;
                    if (executable.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                        throw new IOException("Close the running CoatSim window before uninstalling.");
                }
                catch (Win32Exception) { }
                catch (InvalidOperationException) { }
                finally { process.Dispose(); }
            }

            if (!silent && MessageBox.Show("Remove the CoatSim program files from:\n" + root +
                "\n\nUser data in data, config, history, and artifacts will be kept.",
                "Uninstall CoatSim", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
                return 2;

            foreach (string entry in Directory.EnumerateFileSystemEntries(root).ToArray())
            {
                string name = Path.GetFileName(entry);
                if (String.Equals(name, MarkerName, StringComparison.OrdinalIgnoreCase)) continue;
                if ((File.GetAttributes(entry) & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("A link or junction was found in the install folder. No more files were removed.");
                if (new[] { "data", "config", "history", "artifacts" }.Contains(name, StringComparer.OrdinalIgnoreCase))
                    continue;
                if (Directory.Exists(entry)) Directory.Delete(entry, true);
                else File.Delete(entry);
            }
            File.WriteAllText(marker, UserDataMarkerContent + Environment.NewLine, new UTF8Encoding(false));
            if (!silent) MessageBox.Show("CoatSim program files were removed. User data remains in:\n" + root,
                "CoatSim uninstalled", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 0;
        }
        catch (Exception ex)
        {
            if (!silent) MessageBox.Show(ex.Message, "CoatSim uninstall failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    private sealed class SetupForm : Form
    {
        private readonly TextBox pathBox = new TextBox();
        private readonly Button browseButton = new Button();
        private readonly Button installButton = new Button();
        private readonly Button cancelButton = new Button();
        private readonly CheckBox launchCheck = new CheckBox();
        private readonly Label statusLabel = new Label();
        private readonly ProgressBar progress = new ProgressBar();
        private readonly BackgroundWorker worker = new BackgroundWorker();
        private string installedPath;

        internal SetupForm()
        {
            Text = "CoatSim Offline Installer";
            Width = 600;
            Height = 235;
            MinimumSize = new Size(600, 235);
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;

            Label intro = new Label();
            intro.Text = "Install for the current Windows user. Administrator rights are not required.";
            intro.SetBounds(18, 18, 550, 38);
            Controls.Add(intro);

            Label pathLabel = new Label();
            pathLabel.Text = "Install folder:";
            pathLabel.SetBounds(18, 68, 90, 24);
            Controls.Add(pathLabel);

            pathBox.SetBounds(108, 65, 365, 27);
            pathBox.Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "CoatSim");
            Controls.Add(pathBox);

            browseButton.Text = "Browse...";
            browseButton.SetBounds(482, 64, 86, 29);
            browseButton.Click += Browse;
            Controls.Add(browseButton);

            launchCheck.Text = "Launch CoatSim after installation";
            launchCheck.SetBounds(18, 104, 310, 24);
            launchCheck.Checked = false;
            Controls.Add(launchCheck);

            statusLabel.Text = "";
            statusLabel.SetBounds(18, 135, 550, 24);
            Controls.Add(statusLabel);

            progress.SetBounds(18, 161, 550, 13);
            progress.Style = ProgressBarStyle.Blocks;
            Controls.Add(progress);

            installButton.Text = "Install";
            installButton.SetBounds(375, 179, 92, 30);
            installButton.Click += Install;
            Controls.Add(installButton);

            cancelButton.Text = "Cancel";
            cancelButton.SetBounds(476, 179, 92, 30);
            cancelButton.Click += delegate { Close(); };
            Controls.Add(cancelButton);

            AcceptButton = installButton;
            CancelButton = cancelButton;
            worker.WorkerReportsProgress = true;
            worker.DoWork += InstallWorker;
            worker.ProgressChanged += ProgressChanged;
            worker.RunWorkerCompleted += InstallCompleted;
        }

        private void Browse(object sender, EventArgs e)
        {
            using (FolderBrowserDialog dialog = new FolderBrowserDialog())
            {
                dialog.Description = "Choose the CoatSim installation folder.";
                dialog.ShowNewFolderButton = true;
                string current = pathBox.Text.Trim();
                if (Directory.Exists(current)) dialog.SelectedPath = current;
                else if (Directory.Exists(Path.GetDirectoryName(current))) dialog.SelectedPath = Path.GetDirectoryName(current);
                if (dialog.ShowDialog(this) == DialogResult.OK) pathBox.Text = dialog.SelectedPath;
            }
        }

        private void Install(object sender, EventArgs e)
        {
            string target = pathBox.Text.Trim();
            if (target.Length == 0)
            {
                MessageBox.Show(this, "Choose an installation folder.", Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            try { target = Path.GetFullPath(target); }
            catch (Exception ex)
            {
                MessageBox.Show(this, "The installation path is invalid.\n" + ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            pathBox.Text = target;
            installButton.Enabled = false;
            browseButton.Enabled = false;
            cancelButton.Enabled = false;
            pathBox.Enabled = false;
            launchCheck.Enabled = false;
            progress.Value = 0;
            statusLabel.Text = "Preparing the offline payload...";
            worker.RunWorkerAsync(new object[] { target, launchCheck.Checked });
        }

        private void InstallWorker(object sender, DoWorkEventArgs e)
        {
            object[] args = (object[])e.Argument;
            string target = (string)args[0];
            bool launch = (bool)args[1];
            BackgroundWorker reporter = (BackgroundWorker)sender;
            ExtractPayload(target, reporter);
            installedPath = target;
            e.Result = launch;
        }

        private void ProgressChanged(object sender, ProgressChangedEventArgs e)
        {
            progress.Value = Math.Max(0, Math.Min(100, e.ProgressPercentage));
            statusLabel.Text = e.UserState == null ? "Installing..." : e.UserState.ToString();
        }

        private void InstallCompleted(object sender, RunWorkerCompletedEventArgs e)
        {
            if (e.Error != null)
            {
                statusLabel.Text = "Installation failed.";
                installButton.Enabled = true;
                browseButton.Enabled = true;
                cancelButton.Enabled = true;
                pathBox.Enabled = true;
                launchCheck.Enabled = true;
                MessageBox.Show(this, e.Error.Message, "CoatSim installation failed", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }

            statusLabel.Text = "Installation complete.";
            progress.Value = 100;
            DialogResult answer = MessageBox.Show(this,
                "CoatSim was installed for this Windows user at:\n" + installedPath +
                "\n\nOpen the installation folder now?",
                "CoatSim installed", MessageBoxButtons.YesNo, MessageBoxIcon.Information);
            if (answer == DialogResult.Yes)
                Process.Start(new ProcessStartInfo("explorer.exe", "\"" + installedPath + "\"") { UseShellExecute = false });
            if ((bool)e.Result && File.Exists(Path.Combine(installedPath, "CoatSim.exe")))
                Process.Start(new ProcessStartInfo(Path.Combine(installedPath, "CoatSim.exe")) { WorkingDirectory = installedPath, UseShellExecute = false });
            Close();
        }
    }

    private static void ExtractPayload(string target, BackgroundWorker reporter)
    {
        string fullTarget = Path.GetFullPath(target).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        if (fullTarget.Length < 4 || Path.GetPathRoot(fullTarget).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar).Equals(fullTarget, StringComparison.OrdinalIgnoreCase))
            throw new IOException("Choose a folder below a drive root.");

        bool existed = Directory.Exists(fullTarget);
        if (existed)
        {
            DirectoryInfo info = new DirectoryInfo(fullTarget);
            if ((info.Attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("The selected installation folder is a link or junction.");
            string marker = Path.Combine(fullTarget, MarkerName);
            string progress = Path.Combine(fullTarget, ProgressName);
            if (File.Exists(marker))
            {
                string content = File.ReadAllText(marker, Encoding.UTF8).Trim();
                if (content != MarkerContent && content != UserDataMarkerContent) throw new IOException("The existing install marker is not recognized. Choose another folder.");
            }
            else if (File.Exists(progress))
            {
                string content = File.ReadAllText(progress, Encoding.UTF8).Trim();
                if (content != MarkerContent) throw new IOException("The incomplete install marker is not recognized. Choose another folder.");
            }
            else if (Directory.EnumerateFileSystemEntries(fullTarget).Any())
                throw new IOException("The selected folder is not empty and is not a CoatSim installation. Choose another folder.");
        }

        foreach (Process process in Process.GetProcessesByName("CoatSim"))
        {
            try
            {
                string executable = process.MainModule.FileName;
                if (executable.StartsWith(fullTarget + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                    throw new IOException("Close the running CoatSim window before installing or updating it.");
            }
            catch (Win32Exception) { }
            catch (InvalidOperationException) { }
            finally { process.Dispose(); }
        }

        Directory.CreateDirectory(fullTarget);
        string progressMarker = Path.Combine(fullTarget, ProgressName);
        File.WriteAllText(progressMarker, MarkerContent + Environment.NewLine, new UTF8Encoding(false));
        ListCreatedFiles written = new ListCreatedFiles();
        try
        {
            using (FileStream file = File.OpenRead(Application.ExecutablePath))
            {
                if (file.Length < FooterLength) throw new InvalidDataException("The installer payload footer is missing.");
                file.Position = file.Length - FooterLength;
                byte[] footer = new byte[FooterLength];
                ReadFully(file, footer, 0, footer.Length);
                string magic = Encoding.ASCII.GetString(footer, 0, 8);
                if (magic != Footer) throw new InvalidDataException("The installer payload footer is invalid.");
                long zipLength = BitConverter.ToInt64(footer, 8);
                long zipStart = file.Length - FooterLength - zipLength;
                if (zipLength <= 0 || zipStart < 0) throw new InvalidDataException("The installer payload size is invalid.");

                using (SubStream slice = new SubStream(file, zipStart, zipLength))
                using (ZipArchive archive = new ZipArchive(slice, ZipArchiveMode.Read, true))
                {
                    long allBytes = archive.Entries.Where(x => !String.IsNullOrEmpty(x.Name)).Sum(x => (long)x.Length);
                    long doneBytes = 0;
                    DateTime lastReport = DateTime.MinValue;
                    string rootPrefix = fullTarget + Path.DirectorySeparatorChar;
                    foreach (ZipArchiveEntry entry in archive.Entries)
                    {
                        string relative = entry.FullName.Replace('/', Path.DirectorySeparatorChar).Replace('\\', Path.DirectorySeparatorChar);
                        if (Path.IsPathRooted(relative) || relative.Split(Path.DirectorySeparatorChar).Any(x => x == ".."))
                            throw new InvalidDataException("The payload contains an unsafe path.");
                        string destination = Path.GetFullPath(Path.Combine(fullTarget, relative));
                        if (!destination.StartsWith(rootPrefix, StringComparison.OrdinalIgnoreCase) && !destination.Equals(fullTarget, StringComparison.OrdinalIgnoreCase))
                            throw new InvalidDataException("The payload path escapes the selected folder.");

                        if (String.IsNullOrEmpty(entry.Name))
                        {
                            Directory.CreateDirectory(destination);
                            continue;
                        }
                        if (IsUserData(relative) && File.Exists(destination))
                        {
                            doneBytes += entry.Length;
                            continue;
                        }
                        Directory.CreateDirectory(Path.GetDirectoryName(destination));
                        using (Stream input = entry.Open())
                        using (FileStream output = new FileStream(destination, FileMode.Create, FileAccess.Write, FileShare.None))
                        {
                            byte[] buffer = new byte[1024 * 1024];
                            int count;
                            long local = 0;
                            while ((count = input.Read(buffer, 0, buffer.Length)) > 0)
                            {
                                output.Write(buffer, 0, count);
                                local += count;
                                doneBytes += count;
                                if ((DateTime.UtcNow - lastReport).TotalMilliseconds > 250)
                                {
                                    int percent = allBytes == 0 ? 0 : (int)Math.Min(99, doneBytes * 100 / allBytes);
                                    reporter.ReportProgress(percent, "Installing files... " + (doneBytes / (1024 * 1024)) + " MiB");
                                    lastReport = DateTime.UtcNow;
                                }
                            }
                            if (local != entry.Length) throw new InvalidDataException("A payload file has an unexpected length: " + relative);
                        }
                        if (!written.Contains(destination)) written.Add(destination);
                    }
                }
            }
            File.WriteAllText(Path.Combine(fullTarget, MarkerName), MarkerContent + Environment.NewLine, new UTF8Encoding(false));
            if (File.Exists(progressMarker)) File.Delete(progressMarker);
            reporter.ReportProgress(100, "Installation complete.");
        }
        catch
        {
            foreach (string path in written.AsEnumerable().Reverse())
            {
                try { if (File.Exists(path) && !IsUserData(path.Substring(fullTarget.Length).TrimStart(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar))) File.Delete(path); }
                catch { }
            }
            if (!existed)
            {
                try { if (Directory.Exists(fullTarget) && !Directory.EnumerateFileSystemEntries(fullTarget).Any()) Directory.Delete(fullTarget); }
                catch { }
            }
            throw;
        }
    }

    private static bool IsUserData(string path)
    {
        string normalized = path.Replace('\\', '/');
        return normalized.StartsWith("data/", StringComparison.OrdinalIgnoreCase)
            || normalized.StartsWith("config/", StringComparison.OrdinalIgnoreCase)
            || normalized.StartsWith("history/", StringComparison.OrdinalIgnoreCase)
            || normalized.StartsWith("artifacts/logs/", StringComparison.OrdinalIgnoreCase);
    }

    private static void ReadFully(Stream stream, byte[] buffer, int offset, int count)
    {
        while (count > 0)
        {
            int read = stream.Read(buffer, offset, count);
            if (read == 0) throw new EndOfStreamException();
            offset += read;
            count -= read;
        }
    }

    private sealed class ListCreatedFiles : System.Collections.Generic.List<string> { }

    private sealed class SubStream : Stream
    {
        private readonly Stream source;
        private readonly long start;
        private readonly long length;
        private long position;
        internal SubStream(Stream source, long start, long length) { this.source = source; this.start = start; this.length = length; source.Position = start; }
        public override bool CanRead { get { return source.CanRead; } }
        public override bool CanSeek { get { return source.CanSeek; } }
        public override bool CanWrite { get { return false; } }
        public override long Length { get { return length; } }
        public override long Position { get { return position; } set { Seek(value, SeekOrigin.Begin); } }
        public override int Read(byte[] buffer, int offset, int count)
        {
            if (position >= length) return 0;
            int allowed = (int)Math.Min(count, length - position);
            source.Position = start + position;
            int read = source.Read(buffer, offset, allowed);
            position += read;
            return read;
        }
        public override long Seek(long offset, SeekOrigin origin)
        {
            long next = origin == SeekOrigin.Begin ? offset : (origin == SeekOrigin.Current ? position + offset : length + offset);
            if (next < 0 || next > length) throw new IOException("Attempted to seek outside the payload.");
            position = next;
            source.Position = start + next;
            return position;
        }
        public override void Flush() { }
        public override void SetLength(long value) { throw new NotSupportedException(); }
        public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
    }
}
