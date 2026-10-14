const $ = (selector) => document.querySelector(selector);
let results = [];
let filter = 'all';
let searchController;
let polling = false;
const selected = new Set();
const cancelling = new Set();
const requesting = new Set();
let searchContext = null;
let playlist = null;
let importing = false;
let checking = false;
let queueing = false;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, {...options, headers: {'Content-Type': 'application/json', 'X-Requested-With': 'HomeMusicRequest', ...options.headers}});
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : `Request failed (${response.status})`);
  }
  return response.json();
}

function duration(seconds) {
  if (!seconds) return 'Duration unknown';
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}

function bytes(value) {
  if (!value) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const power = Math.min(3, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** power).toFixed(power ? 1 : 0)} ${units[power]}`;
}

function elapsed(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m ${value % 60}s`;
  return `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`;
}

function renderResults() {
  const context = searchContext;
  const row = context?.row;
  const container = $('#results');
  container.replaceChildren();
  const visible = results.filter((r) => filter === 'all' || r.source === filter);
  if (!visible.length) container.append(element('p', 'muted', 'No recordings found for this source. Try a different search.'));
  for (const result of visible) {
    const card = element('article', 'record');
    const top = element('div', 'record-top');
    if (result.thumbnail) {
      const image = element('img');
      image.src = result.thumbnail;
      image.alt = '';
      image.loading = 'lazy';
      top.append(image);
    }
    const heading = element('div');
    heading.append(element('div', 'source', result.source), element('h3', '', result.title));
    top.append(heading);
    card.append(top);
    if (result.source === 'soulseek') {
      const fields = [result.format.toUpperCase(), result.bitrate ? `${result.bitrate} kbps` : null,
        result.bit_depth ? `${result.bit_depth}-bit` : null, result.sample_rate ? `${result.sample_rate} Hz` : null,
        `${(result.size / 1048576).toFixed(1)} MB`, duration(result.duration)].filter(Boolean);
      card.append(element('div', 'detail', fields.join(' / ')), element('div', 'detail',
        `${result.username} / ${result.free_slot ? 'Free upload slot' : `Queue: ${result.queue ?? 'unknown'}`}`),
        element('div', 'filename', result.filename));
    } else {
      card.append(element('div', 'detail', `${result.channel || 'Unknown channel'} / ${duration(result.duration)}`));
      const link = element('a', 'detail', 'Preview on YouTube');
      link.href = `https://www.youtube.com/watch?v=${encodeURIComponent(result.video_id)}`;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      card.append(link);
    }
    const chosen = row?.choice?.id === result.id;
    const button = element('button', '', row ? (chosen ? 'Chosen Version' : 'Choose This Version') : (selected.has(result.id) ? 'Requested' : 'Download This Version'));
    button.disabled = row ? chosen || row.skipped || row.requested || queueing : selected.has(result.id) || requesting.has(result.id);
    button.addEventListener('click', async () => {
      if (context !== searchContext) return;
      if (row) {
        if (importing || queueing || row.skipped || row.requested) return;
        row.choice = result;
        renderPlaylist();
        renderResults();
        $('#notice').textContent = `Chosen for ${row.track.title}: ${result.source} / ${result.title}. Nothing queued yet.`;
        return;
      }
      if (requesting.has(result.id) || selected.has(result.id)) return;
      if (!$('#permission').checked) {
        $('#notice').textContent = 'Confirm you have permission to download the selected audio.';
        $('#permission').focus();
        return;
      }
      button.disabled = true;
      requesting.add(result.id);
      try {
        await api('/api/downloads', {method: 'POST', body: JSON.stringify({selection_id: result.id, permitted: true})});
        selected.add(result.id);
        button.textContent = 'Requested';
        if (context === searchContext) $('#notice').textContent = 'Request queued. Follow its progress below.';
        refreshJobs();
      } catch (error) {
        if (context === searchContext) $('#notice').textContent = error.message;
        button.disabled = false;
      } finally {
        requesting.delete(result.id);
        if (context === searchContext) renderResults();
      }
    });
    card.append(button);
    container.append(card);
  }
}

function clearSearch() {
  if (searchController) searchController.abort();
  searchController = null;
  searchContext = null;
  results = [];
  $('#results').replaceChildren();
  $('#local-results').replaceChildren();
  $('#results-section').hidden = true;
  $('#local-section').hidden = true;
  $('#playlist-result-context').hidden = true;
  $('#single-permission').hidden = false;
  $('#permission').checked = false;
  $('#search-button').disabled = false;
  $('#search-button').textContent = 'Find Music';
  $('#notice').textContent = '';
}

async function search(query, row = null) {
  clearSearch();
  const context = {row};
  searchContext = context;
  const controller = new AbortController();
  searchController = controller;
  $('#search-button').disabled = !row;
  $('#search-button').textContent = row ? 'Find Music' : 'Searching...';
  $('#playlist-result-context').hidden = !row;
  $('#single-permission').hidden = !!row;
  if (row) {
    $('#version-query').value = query;
    $('#playlist-result-label').textContent = `Versions for playlist track ${playlist.rows.indexOf(row) + 1}: ${row.track.artist} / ${row.track.title} / ${row.track.album || 'Unknown album'}. Choose one exact recording; this does not download it.`;
    $('#results-section').hidden = false;
  }
  $('#notice').textContent = 'Checking your library, Soulseek, and YouTube. This can take up to a minute.';
  renderPlaylist();
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`, {signal: controller.signal});
    if (context !== searchContext) return;
    results = data.results;
    renderResults();
    $('#results-section').hidden = false;
    $('#local-results').replaceChildren();
    for (const track of data.local) {
      $('#local-results').append(element('div', 'local-track', `${track.artist || 'Unknown artist'} / ${track.title} / ${track.album || 'Unknown album'}`));
    }
    $('#local-section').hidden = !data.local.length;
    const errors = Object.entries(data.errors).map(([source, message]) => `${source}: ${message}`);
    $('#notice').textContent = errors.length ? errors.join('. ') : `${results.length} recordings found. ${data.local.length ? 'Possible local matches found.' : 'No matching tracks found in your library.'}`;
  } catch (error) {
    if (context === searchContext && error.name !== 'AbortError') $('#notice').textContent = error.message;
  } finally {
    if (controller === searchController) {
      $('#search-button').disabled = false;
      $('#search-button').textContent = 'Find Music';
      searchController = null;
      renderPlaylist();
    }
  }
}

$('#search-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const query = $('#query').value.trim();
  if (query.length >= 2) search(query);
});

$('#single-search').addEventListener('click', () => {
  clearSearch();
  renderPlaylist();
  $('#query').focus();
});

$('#version-search-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const row = searchContext?.row;
  const query = $('#version-query').value.trim();
  if (row && !importing && !queueing && !row.requested && !row.skipped && query.length >= 2) search(query, row);
});

$('#playlist-back').addEventListener('click', () => {
  const index = playlist?.rows.indexOf(searchContext?.row);
  if (index >= 0) $(`#playlist-track-${index}`).scrollIntoView({block: 'start'});
});

function renderPlaylist() {
  $('#spotify-import').disabled = importing || checking || queueing;
  $('#csv-import').disabled = importing || checking || queueing;
  $('#check-library').disabled = importing || checking || queueing;
  $('#check-library').textContent = checking ? 'Checking Library...' : 'Check Library';
  $('#playlist-review').hidden = !playlist;
  if (!playlist) return;
  $('#playlist-name').textContent = playlist.name;
  $('#playlist-warning').textContent = [playlist.preview ? 'Spotify preview only: this may not be the complete playlist.' : '', playlist.warning || ''].filter(Boolean).join(' ');
  $('#playlist-warning').hidden = !$('#playlist-warning').textContent;
  const container = $('#playlist-tracks');
  container.replaceChildren();
  playlist.rows.forEach((row, index) => {
    const track = row.track;
    const article = element('article', 'playlist-track');
    article.id = `playlist-track-${index}`;
    article.append(element('h3', '', `${index + 1}. ${track.title}`), element('p', 'detail', `${track.artist || 'Unknown artist'} / ${track.album || 'Unknown album'} / ${duration(track.duration)}`));
    const badge = row.requested ? 'Requested' : row.skipped ? 'Skipped' : row.choice ? 'Version chosen' : 'Choose a version';
    article.append(element('span', 'status playlist-badge', badge));
    if (row.checked) {
      if (row.local.length) article.append(element('span', 'status playlist-badge', 'Possible Local Match'));
      article.append(element('p', 'detail', row.local.length ? `${row.local.length} possible library match(es), not confirmed duplicates` : row.libraryError ? 'Library check incomplete' : 'No possible library matches found'));
      for (const local of row.local) article.append(element('p', 'local-track', `${local.artist || 'Unknown artist'} / ${local.title} / ${local.album || 'Unknown album'} / ${duration(local.duration)}`));
      if (row.libraryError) article.append(element('p', 'error', `Library check: ${row.libraryError}`));
    }
    if (row.choice) {
      const choice = row.choice;
      article.append(element('p', 'chosen-version', `Chosen: ${choice.source} / ${choice.title} / ${choice.source === 'soulseek' ? `${choice.username} / ${choice.filename}` : `${choice.channel || 'Unknown channel'} / Video: ${choice.video_id}`} / ${duration(choice.duration)}`));
    }
    const actions = element('div', 'playlist-actions');
    const find = element('button', 'secondary', searchController && searchContext?.row === row ? 'Searching Versions...' : 'Find Versions');
    find.disabled = importing || queueing || row.skipped || row.requested || !!(searchController && searchContext?.row === row);
    find.setAttribute('aria-label', `Find versions for track ${index + 1}: ${track.title}`);
    find.addEventListener('click', () => {
      search(`${track.artist} ${track.title}`.trim().slice(0, 200), row);
      $('#results-section').scrollIntoView({block: 'start'});
    });
    const skip = element('button', 'secondary', row.skipped ? 'Undo Skip' : 'Skip');
    skip.disabled = importing || queueing || row.requested;
    skip.setAttribute('aria-label', `${row.skipped ? 'Undo skip for' : 'Skip'} track ${index + 1}: ${track.title}`);
    skip.addEventListener('click', () => {
      row.skipped = !row.skipped;
      row.choice = null;
      if (searchContext?.row === row) clearSearch();
      renderPlaylist();
    });
    actions.append(find, skip);
    if (row.choice) {
      const remove = element('button', 'secondary', 'Remove Choice');
      remove.disabled = importing || queueing;
      remove.setAttribute('aria-label', `Remove chosen version for track ${index + 1}: ${track.title}`);
      remove.addEventListener('click', () => {
        row.choice = null;
        renderPlaylist();
        renderResults();
      });
      actions.append(remove);
    }
    article.append(actions);
    container.append(article);
  });
  const count = playlist.rows.filter((row) => row.choice && !row.skipped && !row.requested).length;
  $('#playlist-summary').textContent = `${count} chosen / ${playlist.rows.length} imported tracks. ${playlist.rows.filter((row) => row.requested).length} requested.${count > 100 ? ' Remove choices to reach the 100-track batch limit.' : ''}`;
  $('#queue-selected').disabled = importing || checking || queueing || !count || count > 100 || !$('#batch-permission').checked;
  $('#queue-selected').textContent = queueing ? 'Queueing...' : 'Queue Selected';
}

async function importPlaylist(source) {
  if (importing || checking || queueing) return;
  const file = $('#csv-file').files[0];
  if (source === 'csv' && (!file || file.size > 2 * 1024 * 1024)) {
    $('#playlist-notice').textContent = 'Choose a UTF-8 CSV file no larger than 2 MiB.';
    return;
  }
  if (playlist && !window.confirm('Replace this page\'s imported tracks and unqueued choices? Queued jobs will remain.')) return;
  importing = true;
  renderPlaylist();
  $('#playlist-notice').textContent = 'Importing tracks only. No searches or downloads will start.';
  try {
    const options = source === 'csv'
      ? {method: 'POST', headers: {'Content-Type': 'text/csv'}, body: await file.arrayBuffer()}
      : {method: 'POST', body: JSON.stringify({url: $('#spotify-url').value.trim()})};
    const data = await api(`/api/playlists/${source}`, options);
    if (!Array.isArray(data.tracks) || data.tracks.length > 500) throw new Error('Imports must contain at most 500 valid tracks.');
    clearSearch();
    playlist = {...data, rows: data.tracks.map((track) => ({track, choice: null, skipped: false, requested: false, checked: false, local: [], libraryError: null}))};
    $('#batch-permission').checked = false;
    $('#playlist-notice').textContent = `${data.tracks.length} tracks imported. Check Library or Find Versions when ready.`;
  } catch (error) {
    $('#playlist-notice').textContent = error.message;
  } finally {
    importing = false;
    renderPlaylist();
  }
}

$('#spotify-form').addEventListener('submit', (event) => { event.preventDefault(); importPlaylist('spotify'); });
$('#csv-form').addEventListener('submit', (event) => { event.preventDefault(); importPlaylist('csv'); });
$('#batch-permission').addEventListener('change', renderPlaylist);

$('#check-library').addEventListener('click', async () => {
  if (!playlist || importing || checking || queueing) return;
  checking = true;
  renderPlaylist();
  $('#playlist-notice').textContent = 'Checking possible library matches...';
  try {
    const data = await api('/api/playlists/check', {method: 'POST', body: JSON.stringify({tracks: playlist.rows.map((row) => row.track)})});
    if (data.tracks.length !== playlist.rows.length) throw new Error('Library check returned an incomplete track list. Try again.');
    data.tracks.forEach((track, index) => {
      Object.assign(playlist.rows[index], {checked: true, local: track.local || [], libraryError: track.library_error});
    });
    const failures = playlist.rows.filter((row) => row.libraryError).length;
    $('#playlist-notice').textContent = failures ? `Library check finished with ${failures} track error(s). Review the rows or retry Check Library. No choices or skips changed.` : 'Library check complete. Possible matches only; no choices or skips changed.';
  } catch (error) {
    $('#playlist-notice').textContent = error.message;
  } finally {
    checking = false;
    renderPlaylist();
  }
});

$('#queue-selected').addEventListener('click', async () => {
  if (!playlist || importing || checking || queueing) return;
  const rows = playlist.rows.filter((row) => row.choice && !row.skipped && !row.requested);
  if (!rows.length || rows.length > 100 || !$('#batch-permission').checked) return;
  const ids = [...new Set(rows.map((row) => row.choice.id))];
  queueing = true;
  renderPlaylist();
  renderResults();
  $('#playlist-notice').textContent = 'Queueing your chosen recordings...';
  try {
    const data = await api('/api/downloads/batch', {method: 'POST', body: JSON.stringify({selection_ids: ids, permitted: true})});
    rows.forEach((row) => {
      selected.add(row.choice.id);
      row.choice = null;
      row.requested = true;
    });
    $('#batch-permission').checked = false;
    $('#playlist-notice').textContent = `${data.jobs.length} requests queued. Follow progress and cancel below. Queued choices have been cleared.`;
    refreshJobs();
  } catch (error) {
    $('#playlist-notice').textContent = `${error.message}. Choices kept; remove or replace expired versions using Find Versions, then try again.`;
  } finally {
    queueing = false;
    renderPlaylist();
    renderResults();
  }
});

document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => {
  filter = button.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
  renderResults();
}));

async function refreshJobs() {
  if (polling) return;
  polling = true;
  try {
    const jobs = await api('/api/downloads');
    const container = $('#jobs');
    container.replaceChildren();
    if (!jobs.length) container.append(element('p', 'muted', 'No requests yet. Find something worth keeping.'));
    for (const job of jobs) {
      const row = element('article', 'job');
      const heading = element('div', 'job-heading');
      const actions = element('div', 'job-actions');
      actions.append(element('span', 'status', job.status));
      if (job.status === 'Queued' || job.status === 'Downloading') {
        const button = element('button', 'secondary cancel-button', cancelling.has(job.id) ? 'Cancelling...' : 'Cancel');
        button.disabled = cancelling.has(job.id);
        button.setAttribute('aria-label', `Cancel ${job.title}`);
        button.addEventListener('click', async () => {
          cancelling.add(job.id);
          button.disabled = true;
          button.textContent = 'Cancelling...';
          try {
            const result = await api(`/api/downloads/${encodeURIComponent(job.id)}/cancel`, {method: 'POST'});
            $('#notice').textContent = result.status === 'Cancelled' ? 'Request cancelled.' : 'Stopping download. Waiting for cancellation to be confirmed.';
          } catch (error) {
            $('#notice').textContent = error.message;
          } finally {
            cancelling.delete(job.id);
            refreshJobs();
          }
        });
        actions.append(button);
      }
      heading.append(element('span', 'job-title', job.title), actions);
      row.append(heading);
      if (job.status === 'Downloading' || job.status === 'Processing') {
        const progress = element('progress');
        progress.max = 100;
        progress.setAttribute('aria-label', `${job.title} download progress`);
        if (job.total_bytes || job.progress > 0) progress.value = job.progress;
        row.append(progress);
        const metrics = element('div', 'transfer-metrics');
        if (job.status === 'Downloading') {
          metrics.append(element('span', 'transfer-percent', job.total_bytes ? `${job.progress.toFixed(1)}%` : 'Size unknown'));
          metrics.append(element('span', '', `${bytes(job.downloaded_bytes)}${job.total_bytes ? ` / ${bytes(job.total_bytes)}` : ' downloaded'}`));
          metrics.append(element('span', '', `${bytes(job.speed)}/s`));
          if (job.eta !== null && job.eta !== undefined && job.speed > 0) {
            metrics.append(element('span', '', `About ${elapsed(job.eta)} remaining`));
          }
          metrics.append(element('span', '', job.downloaded_bytes > 0
            ? `Last data: ${elapsed(job.idle_seconds)} ago` : `Waiting for first data: ${elapsed(job.idle_seconds)}`));
        } else {
          metrics.append(element('span', '', 'Audio downloaded. Processing and importing...'));
        }
        row.append(metrics);
        if (job.waiting_for_peer) {
          row.append(element('p', 'detail', `Waiting for Soulseek peer${job.queue_position ? ` / Queue position: ${job.queue_position}` : ''}. No data is expected until the peer starts sending.`));
        } else if (job.stalled) {
          row.append(element('p', 'stall-warning', `Possibly stalled: no new data for ${elapsed(job.idle_seconds)}. The request is still active; it may recover or time out.`));
        }
        if (job.transfer_state) row.append(element('p', 'detail', job.transfer_state));
        const updateAge = job.last_update ? Date.now() / 1000 - job.last_update : 0;
        if (updateAge >= 20) row.append(element('p', 'detail error', `Transfer status has not updated for ${elapsed(updateAge)}.`));
      } else if (job.status === 'Queued') {
        row.append(element('p', 'detail', 'Waiting for earlier requests to finish.'));
      } else if (job.status === 'Cancelling') {
        row.append(element('p', 'detail', 'Stopping the transfer. This track will not be imported.'));
      } else if (job.status === 'Cancelled') {
        row.append(element('p', 'detail', 'Cancelled. No track was imported. Search again to request another version.'));
      }
      if (job.path) row.append(element('p', 'detail', `Library: ${job.path}`));
      if (job.error || job.warning) row.append(element('p', 'detail error', job.error || job.warning));
      container.append(row);
    }
  } catch (error) {
    $('#jobs').replaceChildren(element('p', 'error', `Unable to refresh requests: ${error.message}`));
  } finally {
    polling = false;
  }
}

$('#scan-button').addEventListener('click', async () => {
  $('#scan-button').disabled = true;
  try {
    await api('/api/scan', {method: 'POST'});
    $('#notice').textContent = 'Navidrome scan requested. Tracks will appear when the scan finishes.';
  } catch (error) {
    $('#notice').textContent = error.message;
  } finally {
    $('#scan-button').disabled = false;
  }
});
refreshJobs();
setInterval(() => { if (!document.hidden) refreshJobs(); }, 2500);
