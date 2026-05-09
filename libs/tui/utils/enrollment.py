#!/usr/bin/env python3
"""Enrollment utility functions for AIG CLI TUI"""

import logging
import os
import pexpect
from enum import Enum

from libs.tui.tui_actions import TUIActions


logger = logging.getLogger(__name__)


class EnrollmentStatus(Enum):
    """Enrollment operation status codes"""
    SUCCESS = "success"
    ALREADY_ENROLLED = "already_enrolled"
    MENU_ITEM_NOT_FOUND = "menu_item_not_found"
    ENV_VAR_NOT_SET = "env_var_not_set"
    PRE_ENROLLMENT_TIMEOUT = "pre_enrollment_timeout"
    PRE_ENROLLMENT_EXCEPTION = "pre_enrollment_exception"
    ENROLLMENT_FAILED = "enrollment_failed"
    ENROLLMENT_TIMEOUT = "enrollment_timeout"
    ENROLLMENT_EXCEPTION = "enrollment_exception"


def enrollment(tui_session):
    """
    Perform enrollment for AIG CLI TUI.

    Args:
        tui_session: The TUI session instance

    Returns:
        tuple: (EnrollmentStatus, error_message)
            - EnrollmentStatus: Status code indicating the result
            - error_message: Error description string (None if successful)

    Example:
        status, error = enrollment(tui_session)
        if status in [EnrollmentStatus.SUCCESS, EnrollmentStatus.ALREADY_ENROLLED]:
            print("Enrollment succeeded")
        else:
            print(f"Enrollment failed: {error}")
    """
    # Create TUIActions instance for menu navigation
    actions = TUIActions(tui_session)

    # Get current screen content
    tui_session.screen.capture_screenshot("main_menu")

    # Navigate to enrollment menu item
    if not actions.navigate_to_menu_item("Enroll this AI Gateway"):
        return EnrollmentStatus.MENU_ITEM_NOT_FOUND, "Could not find 'Enroll this AI Gateway' menu item"

    logger.info("Navigated to 'Enroll this AI Gateway' screen successfully, pressing Enter to continue...")
    tui_session.press_enter()

    # Wait for enrollment token input page with 15 minute timeout
    timeout = 900  # 15 minutes in seconds
    patterns = [
        r'Enter enrollment token:',
        r'Enrollment completed',
        pexpect.TIMEOUT
    ]

    logger.info(f"Waiting for 'Enter enrollment token:' (timeout: {timeout // 60} minutes)...")

    # Use pexpect's expect to wait for the enrollment token to prevent get_screen_text() keep old screen data
    try:
        # Wait for either the token input or enrollment completed message
        logger.info("Waiting for enrollment token to appear...")
        page_index = tui_session.child.expect(patterns, timeout=timeout)
        if page_index == 0:
            logger.info(f"Found enrollment token input page")
            tui_session.screen.capture_screenshot("pre_enrollment_completed")
        elif page_index == 1:
            logger.info("Enrollment already completed message detected.")
            tui_session.screen.capture_screenshot("pre_enrollment_already_completed")
            return EnrollmentStatus.ALREADY_ENROLLED, None
        else:  # Timeout
            screen_text = tui_session.get_screen_text()
            logger.error(f"Timeout waiting for pre-enrollment finished. Current screen:\n{screen_text}")
            tui_session.screen.capture_screenshot("pre_enrollment_timeout")
            error_msg = f"Timeout waiting for pre-enrollment. Current screen: {screen_text}"
            return EnrollmentStatus.PRE_ENROLLMENT_TIMEOUT, error_msg

    except Exception as e:
        logger.error(f"Exception while waiting for pre-enrollment: {e}")
        tui_session.screen.capture_screenshot("pre_enrollment_error")
        return EnrollmentStatus.PRE_ENROLLMENT_EXCEPTION, f"Exception while waiting for pre-enrollment: {str(e)}"

    # Get enrollment token from environment variable
    enrollment_token = os.getenv("ENROLLMENT_TOKEN")
    if not enrollment_token:
        return EnrollmentStatus.ENV_VAR_NOT_SET, "ENROLLMENT_TOKEN environment variable is not set"

    logger.info(f"Using enrollment token: {enrollment_token}")

    # Paste the enrollment token
    logger.info("Pasting enrollment token...")
    tui_session.child.send(enrollment_token)

    # Press Enter to submit
    logger.info("Pressing Enter to submit enrollment token...")
    tui_session.press_enter()

    # Capture screen after submission
    tui_session.screen.capture_screenshot("enrollment_token_submitted")

    # Wait for enrollment result
    result_patterns = [
        r'Enrollment completed',
        r'error',
        pexpect.TIMEOUT
    ]

    logger.info("Waiting for enrollment result...")
    try:
        result_index = tui_session.child.expect(result_patterns, timeout=60)

        if result_index == 0:  # Success pattern
            logger.info("Enrollment completed successfully!")
            tui_session.screen.capture_screenshot("enrollment_success")
        elif result_index == 1:  # Error pattern
            screen_text = tui_session.get_screen_text()
            logger.error(f"Enrollment failed. Screen content:\n{screen_text}")
            tui_session.screen.capture_screenshot("enrollment_failed")
            error_msg = f"Enrollment failed - invalid token or error occurred. Screen: {screen_text}"
            return EnrollmentStatus.ENROLLMENT_FAILED, error_msg
        else:  # Timeout
            screen_text = tui_session.get_screen_text()
            logger.warning(f"Timeout waiting for enrollment result. Screen content:\n{screen_text}")
            tui_session.screen.capture_screenshot("enrollment_result_timeout")
            error_msg = f"Timeout waiting for enrollment result. Screen: {screen_text}"
            return EnrollmentStatus.ENROLLMENT_TIMEOUT, error_msg

    except Exception as e:
        logger.error(f"Exception while waiting for enrollment result: {e}")
        tui_session.screen.capture_screenshot("enrollment_result_error")
        return EnrollmentStatus.ENROLLMENT_EXCEPTION, f"Exception while waiting for enrollment result: {str(e)}"

    logger.info("Enrollment completed successfully")
    return EnrollmentStatus.SUCCESS, None
