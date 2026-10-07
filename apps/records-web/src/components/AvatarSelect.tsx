import { Select } from "@base-ui/react/select";
import { useId, useState } from "react";

import type { AvatarRef } from "../api/types";
import styles from "../styles/home.module.css";
import { Avatar } from "./Avatar";

export interface AvatarSelectOption<Value extends string> {
  readonly value: Value;
  readonly label: string;
  readonly avatar: AvatarRef | null;
}

function OptionAvatar({ avatar }: { readonly avatar: AvatarRef | null }) {
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
  onChange,
}: {
  readonly label: string;
  readonly value: Value;
  readonly options: readonly AvatarSelectOption<Value>[];
  readonly onChange: (value: Value) => void;
}) {
  const id = useId();
  const [instant, setInstant] = useState(false);
  const selected = options.find((option) => option.value === value) ?? options[0];
  return (
    <div className={styles.filterField}>
      <span id={`${id}-label`}>{label}</span>
      <Select.Root
        onOpenChange={(_open, details) =>
          setInstant(
            details.event.type.startsWith("key") ||
              ("detail" in details.event && details.event.detail === 0),
          )
        }
        value={value}
        items={options}
        onValueChange={(next) => {
          if (next !== null) onChange(next);
        }}
      >
        <Select.Trigger className={styles.avatarSelectButton} aria-labelledby={`${id}-label`}>
          <OptionAvatar avatar={selected?.avatar ?? null} />
          <Select.Value />
          <Select.Icon className={styles.selectChevron}>▾</Select.Icon>
        </Select.Trigger>
        <Select.Portal>
          <section aria-label={`${label}の選択肢`}>
            <Select.Positioner
              className={styles.avatarSelectPositioner}
              sideOffset={8}
              alignItemWithTrigger={false}
            >
              <Select.Popup className={styles.avatarSelectMenu} data-instant={instant}>
                <Select.List aria-labelledby={`${id}-label`}>
                  {options.map((option) => (
                    <Select.Item
                      className={styles.avatarSelectOption}
                      key={option.value || "all"}
                      value={option.value}
                    >
                      <OptionAvatar avatar={option.avatar} />
                      <Select.ItemText>{option.label}</Select.ItemText>
                      <Select.ItemIndicator className={styles.selectedCheck}>
                        ✓
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
