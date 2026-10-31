// Runs only with --smoke-test, which forces the engine's pretend phone.
void (async () => {
  const report = (result) => window.webkit.messageHandlers.smokeResult.postMessage(result);
  try {
    const {getState, setState} = await import('/static/js/store.js');
    const actions = await import('/static/js/actions.js');
    const {get} = await import('/static/js/api.js');
    const waitFor = async (predicate) => {
      for (let attempt = 0; attempt < 300; attempt++) {
        if (predicate()) return;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
      throw new Error('Timed out waiting for the editor');
    };
    const check = (condition, message) => { if (!condition) throw new Error(message); };
    await waitFor(() => getState().draft && getState().job?.state !== 'running');
    check(getState().server.demo === true, 'Smoke test must use a pretend phone');
    check(document.querySelector('.phone'), 'Phone preview did not render');
    const count = getState().draft.pages.length;
    actions.addPage();
    check(getState().draft.pages.length === count + 1, 'Add page failed');
    actions.undo();
    check(getState().draft.pages.length === count, 'Undo failed');
    const original = JSON.stringify(getState().draft.pages[0]);
    const draft = structuredClone(getState().draft);
    const items = draft.pages[0];
    [items[0], items[1]] = [items[1], items[0]];
    actions.commit(draft);
    await waitFor(() => !getState().check.pending && getState().check.dirty);
    check(getState().check.ok, 'Reordered layout did not validate');
    actions.openApply();
    check(getState().dialog?.kind === 'apply', 'Apply preview did not open');
    await actions.startApply();
    check(getState().job?.result?.status === 'applied', 'Demo apply did not verify');
    const applyJob = getState().job.id;
    await waitFor(() => !getState().check.pending && !getState().check.dirty);
    const {backups} = await get('/api/backups');
    check(backups.length > 0, 'Apply did not save a backup');
    await actions.restoreBackup(backups[0].name);
    const restore = getState().job;
    check(restore?.id !== applyJob && restore?.kind === 'restore', 'Demo backup restore did not start');
    check(restore.result?.status === 'applied', 'Demo backup restore did not verify');
    check(JSON.stringify(getState().draft.pages[0]) === original, 'Restore did not bring back the original layout');
    setState({dialog: null});
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    await new Promise(resolve => setTimeout(resolve, 300)); // Let icon position transitions finish before the snapshot.
    check(document.querySelector('.brand-name')?.textContent === 'Rejig', 'Native editor bridge did not run');
    report({ok: true, checks: ['WebKit modules', 'authenticated API', 'phone preview', 'add page', 'undo', 'reorder', 'validation', 'apply preview', 'backup', 'apply and read-back verification', 'backup restore']});
  } catch (error) { report({ok: false, error: String(error)}); }
})();
