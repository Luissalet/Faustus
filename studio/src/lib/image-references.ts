import type {Attachment} from '../adapters/composer';
import {t} from '../i18n';

export const REFERENCE_ROLES = [
  {value:'subject', label:'Subject or character'},
  {value:'style', label:'Visual style'},
  {value:'composition', label:'Composition'},
] as const;

/** Guidance is visible user text, not a hidden system instruction or a promise of model conditioning. */
export function withImageReferences(message:string, attachments:Attachment[]):string {
  let imageIndex=0;
  const lines=attachments.flatMap(attachment=>{
    if(!attachment.mime.startsWith('image/')) return [];
    imageIndex++;
    const role=REFERENCE_ROLES.find(role=>role.value===attachment.referenceRole);
    if(!role) return [];
    return [t('Image {n} ({name}): use as a reference for {role}.', {
      n:imageIndex, name:JSON.stringify(attachment.name), role:t(role.label),
    })];
  });
  return lines.length ? [message,t('Image reference guidance:'),...lines].filter(Boolean).join('\n\n') : message;
}
