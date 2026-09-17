import { useEffect, useState } from 'react';
import { Button } from '../../components';
import { Field, Select } from '../settings/fields';
import { FormFoot, useMsg } from '../settings/IntegrationForms';
import {
  createConnector,
  listLaunchProfiles,
  stateLabel,
  updateConnector,
  type LaunchProfile,
  type Connector,
  type ConnectorPreset,
} from '../../adapters/connectors';
import { t } from '../../i18n';

/**
 * Alta desde preset: one field per `preset.placeholders`, pre-filled from
 * `preset.defaults` only — the preset itself never carries a path of Luis's
 * own machine (CONTRATO_CONECTORES principle 7), so there is nothing here
 * to scrub. Editing an existing connector reuses the same fields; the
 * preset id never changes after creation.
 */
export function NewConnectorForm({
  preset,
  existing,
  initialValues,
  onClose,
  onSaved,
}: {
  preset: ConnectorPreset;
  existing?: Connector;
  /** Prefills from a "Nearby apps" discovery hit: what discovery could
   *  already fill in, merged over the preset's own defaults. Only the
   *  fields discovery listed in `missing` are usually still empty. */
  initialValues?: Record<string, string>;
  onClose: () => void;
  onSaved: (c: Connector) => void;
}) {
  const [name, setName] = useState(existing?.server.name ?? preset.name);
  const [values, setValues] = useState<Record<string, string>>(() => {
    const v: Record<string, string> = {};
    for (const p of preset.placeholders) v[p] = existing?.values[p] ?? initialValues?.[p] ?? preset.defaults?.[p] ?? '';
    return v;
  });
  // Which user-made launch profile "Start the app" / "Open the app" run
  // for this connector (F1.4). '' = none; the list is the user's own
  // profiles, the agent can neither create one nor pick one here.
  const [profileId, setProfileId] = useState<string>(existing?.launch_profile_id ?? '');
  const [profiles, setProfiles] = useState<LaunchProfile[]>([]);
  useEffect(() => {
    let live = true;
    listLaunchProfiles()
      .then((list) => { if (live) setProfiles(list); })
      .catch(() => { /* the field still shows the current id; the panel reports errors */ });
    return () => { live = false; };
  }, []);
  const [busy, setBusy] = useState(false);
  const m = useMsg();

  const missing = preset.placeholders.filter((p) => !(values[p] ?? '').trim());

  const save = async () => {
    m.clear();
    if (!name.trim()) return m.bad(t('A name is required.'));
    if (missing.length) return m.bad(t('Fill in: {list}', { list: missing.join(', ') }));
    setBusy(true);
    try {
      const launch_profile_id = profileId || null;
      const c = existing
        ? await updateConnector(existing.id, { values, name: name.trim(), launch_profile_id })
        : await createConnector({ preset_id: preset.id, values, name: name.trim(), launch_profile_id });
      m.good(t('Saved. Status: {state}', { state: stateLabel(c.status.state) }));
      onSaved(c);
    } catch (e) {
      m.bad((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <h3 className="fs-set__card-title">{existing ? t('Edit connector') : t('New connector: {name}', { name: preset.name })}</h3>
      <p className="fs-prose">{t(preset.purpose)}</p>
      {preset.capabilities.length > 0 && (
        <p className="fs-set__help">{t('Capabilities: {list}', { list: preset.capabilities.join(', ') })}</p>
      )}
      <Field label={t('Name')} htmlFor="conn-name">
        <input id="conn-name" className="fs-field" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      {preset.placeholders.map((p) => (
        <Field key={p} label={p} htmlFor={`conn-ph-${p}`}>
          <input
            id={`conn-ph-${p}`}
            className="fs-field"
            value={values[p] ?? ''}
            onChange={(e) => setValues((v) => ({ ...v, [p]: e.target.value }))}
            placeholder={preset.defaults?.[p] ?? ''}
            autoComplete="off"
          />
        </Field>
      ))}
      <Field label={t('Launch profile')} htmlFor="conn-launch-profile" help={t('What "Start the app" and "Open the app" run. Create profiles under "Launch profiles".')}>
        <Select
          id="conn-launch-profile"
          value={profileId}
          onChange={setProfileId}
          allowEmpty={t('None')}
          options={profiles.map((p) => ({ value: p.id, label: `${p.name} (${p.kind})` }))}
        />
      </Field>
      {preset.launch_profile_hint && (
        <details className="fs-modes__prompt">
          <summary>{t('Launch profile hint (for a launch profile you create yourself)')}</summary>
          <pre className="fs-set__pre">{JSON.stringify(preset.launch_profile_hint, null, 2)}</pre>
        </details>
      )}
      <FormFoot msg={m.msg} tone={m.tone}>
        <Button size="sm" variant="ghost" label={t('Close')} onClick={onClose} disabled={busy} />
        <Button size="sm" variant="primary" label={existing ? t('Save') : t('Create')} loading={busy} onClick={() => void save()} testId="connector-form-save" />
      </FormFoot>
    </>
  );
}
