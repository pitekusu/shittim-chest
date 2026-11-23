import { Select } from "@base-ui/react/select";
import { useId, useState, type ReactNode } from "react";

import type { AvatarRef } from "../api/types";
import commonStyles from "../styles/common.module.css";
import styles from "../styles/home.module.css";
import { Avatar } from "./Avatar";

export interface AvatarSelectOption<Value extends string> {
  readonly value: Value;
  readonly label: string;
  readonly valueAriaLabel?: string;
  readonly avatar: AvatarRef | null;
  readonly icon?: ReactNode;
}

function OptionLabel({
  label,
  valueAriaLabel,
}: Pick<AvatarSelectOption<string>, "label" | "valueAriaLabel">) {
  if (!valueAriaLabel) return label;
  return (
    <>
      <span aria-hidden="true">{label}</span>
      <span className={commonStyles.visuallyHidden}>{valueAriaLabel}</span>
    </>
  );
}

function OptionIcon({ avatar, icon }: Pick<AvatarSelectOption<string>, "avatar" | "icon">) {
  if (icon != null) {
    return (
      <span className={styles.filterIcon} aria-hidden="true">
        {icon}
      </span>
    );
  }
  return avatar ? (
    <span className={styles.filterAvatar} aria-hidden="true">
      <Avatar avatar={avatar} />
    </span>
  ) : (
    <span className={styles.filterAllIcon} aria-hidden="true">
      ◇
    </span>
  );
}

export function AvatarSelect<Value extends string>({
  label,
  value,
  options,
  animatedChevron = false,
  status,
  onOpen,
  onChange,
}: {
  readonly label: string;
  readonly value: Value;
  readonly options: readonly AvatarSelectOption<Value>[];
  readonly animatedChevron?: boolean;
  readonly status?: string;
  readonly onOpen?: () => void;
  readonly onChange: (value: Value) => void;
}) {
  const id = useId();
  const [instant, setInstant] = useState(false);
  const selected = options.find((option) => option.value === value) ?? options[0];
  return (
    <div className={styles.filterField}>
      <span id={`${id}-label`}>{label}</span>
      <Select.Root
        onOpenChange={(open, { event }) => {
          setInstant(
            event.type.startsWith("key") ||
              (event.type === "click" && "detail" in event && event.detail === 0),
          );
          if (open) onOpen?.();
        }}
        value={value}
        items={options}
        onValueChange={(next) => {
          if (next !== null) onChange(next);
        }}
      >
        <Select.Trigger
          className={styles.avatarSelectButton}
          aria-labelledby={`${id}-label`}
          aria-describedby={selected?.valueAriaLabel ? `${id}-value` : undefined}
        >
          <OptionIcon avatar={selected?.avatar ?? null} icon={selected?.icon} />
          <Select.Value id={`${id}-value`}>
            {selected?.valueAriaLabel && (
              <OptionLabel label={selected.label} valueAriaLabel={selected.valueAriaLabel} />
            )}
          </Select.Value>
          <Select.Icon
            className={styles.selectChevron}
            data-animate-chevron={animatedChevron && !instant ? "" : undefined}
          >
            <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
              <path d="m4 6 4 4 4-4Z" fill="currentColor" />
            </svg>
          </Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <section aria-label={`${label}の選択肢`}>
            <Select.Positioner
              className={styles.avatarSelectPositioner}
              sideOffset={8}
              alignItemWithTrigger={false}
            >
              <Select.Popup className={styles.avatarSelectMenu} data-instant={instant}>
                {onOpen && <output className={styles.avatarSelectStatus}>{status}</output>}
                <Select.List aria-labelledby={`${id}-label`}>
                  {options.map((option) => (
                    <Select.Item
                      className={styles.avatarSelectOption}
                      key={option.value || "all"}
                      value={option.value}
                      label={option.valueAriaLabel}
                    >
                      <OptionIcon avatar={option.avatar} icon={option.icon} />
                      <Select.ItemText>
                        <OptionLabel label={option.label} valueAriaLabel={option.valueAriaLabel} />
                      </Select.ItemText>
                      <Select.ItemIndicator className={styles.selectedCheck}>
                        <svg
                          viewBox="0 0 24 24"
                          width="18"
                          height="18"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          aria-hidden="true"
                          focusable="false"
                        >
                          <path d="m5 12 4 4L19 6" />
                        </svg>
                      </Select.ItemIndicator>
                    </Select.Item>
                  ))}
                </Select.List>
              </Select.Popup>
            </Select.Positioner>
          </section>
        </Select.Portal>
      </Select.Root>
    </div>
  );
}
