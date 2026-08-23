import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

import type { AvatarRef } from "../api/types";
import styles from "../styles/home.module.css";
import { Avatar } from "./Avatar";

/* oxlint-disable jsx-a11y/prefer-tag-over-role -- Native select options cannot render avatars consistently. */

export interface AvatarSelectOption<Value extends string> {
  readonly value: Value;
  readonly label: string;
  readonly avatar: AvatarRef | null;
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
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [open, setOpen] = useState(false);
  const selectedIndex = Math.max(
    0,
    options.findIndex((option) => option.value === value),
  );
  const [activeIndex, setActiveIndex] = useState(selectedIndex);
  const selected = options[selectedIndex] ?? options[0];

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    optionRefs.current[activeIndex]?.focus();
  }, [activeIndex, open]);

  const openAt = (index: number) => {
    setActiveIndex(index);
    setOpen(true);
  };
  const closeAndFocusTrigger = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };
  const moveFocus = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | undefined;
    if (event.key === "ArrowDown") nextIndex = (index + 1) % options.length;
    if (event.key === "ArrowUp") nextIndex = (index - 1 + options.length) % options.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = options.length - 1;
    if (nextIndex !== undefined) {
      event.preventDefault();
      setActiveIndex(nextIndex);
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeAndFocusTrigger();
    }
    if (event.key === "Tab") setOpen(false);
  };

  return (
    <div className={styles.filterField} ref={rootRef}>
      <span id={`${id}-label`}>{label}</span>
      <div className={styles.avatarSelect}>
        <button
          ref={triggerRef}
          className={styles.avatarSelectButton}
          type="button"
          aria-label={label}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={`${id}-listbox`}
          aria-describedby={`${id}-value`}
          onClick={() => (open ? setOpen(false) : openAt(selectedIndex))}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              openAt(event.key === "ArrowDown" ? selectedIndex : options.length - 1);
            }
          }}
        >
          {selected?.avatar ? (
            <span className={styles.filterAvatar} aria-hidden="true">
              <Avatar avatar={selected.avatar} />
            </span>
          ) : (
            <span className={styles.filterAllIcon} aria-hidden="true">
              ◇
            </span>
          )}
          <span id={`${id}-value`}>{selected?.label ?? "すべて"}</span>
          <span className={styles.selectChevron} aria-hidden="true">
            ▾
          </span>
        </button>
        {open && (
          <div
            id={`${id}-listbox`}
            className={styles.avatarSelectMenu}
            role="listbox"
            aria-labelledby={`${id}-label`}
          >
            {options.map((option, index) => (
              <button
                key={option.value || "all"}
                ref={(node) => {
                  optionRefs.current[index] = node;
                }}
                className={styles.avatarSelectOption}
                type="button"
                role="option"
                aria-selected={option.value === value}
                tabIndex={index === activeIndex ? 0 : -1}
                onKeyDown={(event) => moveFocus(event, index)}
                onClick={() => {
                  onChange(option.value);
                  closeAndFocusTrigger();
                }}
              >
                {option.avatar ? (
                  <span className={styles.filterAvatar} aria-hidden="true">
                    <Avatar avatar={option.avatar} />
                  </span>
                ) : (
                  <span className={styles.filterAllIcon} aria-hidden="true">
                    ◇
                  </span>
                )}
                <span>{option.label}</span>
                {option.value === value && (
                  <span className={styles.selectedCheck} aria-hidden="true">
                    ✓
                  </span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* oxlint-enable jsx-a11y/prefer-tag-over-role */
